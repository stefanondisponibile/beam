#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""A runner implementation that submits a job for remote execution.

The runner will create a JSON description of the job graph and then submit it
to the Dataflow Service for remote execution by a worker.
"""
from __future__ import absolute_import
from __future__ import division

import base64
import json
import logging
import sys
import threading
import time
import traceback
import urllib
from builtins import hex
from collections import defaultdict

from future.utils import iteritems

import apache_beam as beam
from apache_beam import coders
from apache_beam import error
from apache_beam import pvalue
from apache_beam.internal import pickler
from apache_beam.internal.gcp import json_value
from apache_beam.options.pipeline_options import DebugOptions
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.options.pipeline_options import StandardOptions
from apache_beam.options.pipeline_options import TestOptions
from apache_beam.options.pipeline_options import WorkerOptions
from apache_beam.portability import common_urns
from apache_beam.pvalue import AsSideInput
from apache_beam.runners.dataflow.internal import names
from apache_beam.runners.dataflow.internal.clients import dataflow as dataflow_api
from apache_beam.runners.dataflow.internal.names import PropertyNames
from apache_beam.runners.dataflow.internal.names import TransformNames
from apache_beam.runners.runner import PipelineResult
from apache_beam.runners.runner import PipelineRunner
from apache_beam.runners.runner import PipelineState
from apache_beam.runners.runner import PValueCache
from apache_beam.transforms import window
from apache_beam.transforms.display import DisplayData
from apache_beam.typehints import typehints
from apache_beam.utils import proto_utils
from apache_beam.utils.plugin import BeamPlugin

try:                    # Python 3
  unquote_to_bytes = urllib.parse.unquote_to_bytes
  quote = urllib.parse.quote
except AttributeError:  # Python 2
  # pylint: disable=deprecated-urllib-function
  unquote_to_bytes = urllib.unquote
  quote = urllib.quote


__all__ = ['DataflowRunner']


class DataflowRunner(PipelineRunner):
  """A runner that creates job graphs and submits them for remote execution.

  Every execution of the run() method will submit an independent job for
  remote execution that consists of the nodes reachable from the passed in
  node argument or entire graph if node is None. The run() method returns
  after the service created the job and  will not wait for the job to finish
  if blocking is set to False.
  """

  # A list of PTransformOverride objects to be applied before running a pipeline
  # using DataflowRunner.
  # Currently this only works for overrides where the input and output types do
  # not change.
  # For internal SDK use only. This should not be updated by Beam pipeline
  # authors.

  # Imported here to avoid circular dependencies.
  # TODO: Remove the apache_beam.pipeline dependency in CreatePTransformOverride
  from apache_beam.runners.dataflow.ptransform_overrides import CreatePTransformOverride
  from apache_beam.runners.dataflow.ptransform_overrides import ReadPTransformOverride

  _PTRANSFORM_OVERRIDES = [
      CreatePTransformOverride(),
  ]

  _SDF_PTRANSFORM_OVERRIDES = [
      ReadPTransformOverride(),
  ]

  def __init__(self, cache=None):
    # Cache of CloudWorkflowStep protos generated while the runner
    # "executes" a pipeline.
    self._cache = cache if cache is not None else PValueCache()
    self._unique_step_id = 0

  def is_fnapi_compatible(self):
    return False

  def _get_unique_step_name(self):
    self._unique_step_id += 1
    return 's%s' % self._unique_step_id

  @staticmethod
  def poll_for_job_completion(runner, result, duration):
    """Polls for the specified job to finish running (successfully or not).

    Updates the result with the new job information before returning.

    Args:
      runner: DataflowRunner instance to use for polling job state.
      result: DataflowPipelineResult instance used for job information.
      duration (int): The time to wait (in milliseconds) for job to finish.
        If it is set to :data:`None`, it will wait indefinitely until the job
        is finished.
    """
    last_message_time = None
    current_seen_messages = set()

    last_error_rank = float('-inf')
    last_error_msg = None
    last_job_state = None
    # How long to wait after pipeline failure for the error
    # message to show up giving the reason for the failure.
    # It typically takes about 30 seconds.
    final_countdown_timer_secs = 50.0
    sleep_secs = 5.0

    # Try to prioritize the user-level traceback, if any.
    def rank_error(msg):
      if 'work item was attempted' in msg:
        return -1
      elif 'Traceback' in msg:
        return 1
      return 0

    if duration:
      start_secs = time.time()
      duration_secs = duration // 1000

    job_id = result.job_id()
    while True:
      response = runner.dataflow_client.get_job(job_id)
      # If get() is called very soon after Create() the response may not contain
      # an initialized 'currentState' field.
      if response.currentState is not None:
        if response.currentState != last_job_state:
          logging.info('Job %s is in state %s', job_id, response.currentState)
          last_job_state = response.currentState
        if str(response.currentState) != 'JOB_STATE_RUNNING':
          # Stop checking for new messages on timeout, explanatory
          # message received, success, or a terminal job state caused
          # by the user that therefore doesn't require explanation.
          if (final_countdown_timer_secs <= 0.0
              or last_error_msg is not None
              or str(response.currentState) == 'JOB_STATE_DONE'
              or str(response.currentState) == 'JOB_STATE_CANCELLED'
              or str(response.currentState) == 'JOB_STATE_UPDATED'
              or str(response.currentState) == 'JOB_STATE_DRAINED'):
            break

          # Check that job is in a post-preparation state before starting the
          # final countdown.
          if (str(response.currentState) not in (
              'JOB_STATE_PENDING', 'JOB_STATE_QUEUED')):
            # The job has failed; ensure we see any final error messages.
            sleep_secs = 1.0      # poll faster during the final countdown
            final_countdown_timer_secs -= sleep_secs

      time.sleep(sleep_secs)

      # Get all messages since beginning of the job run or since last message.
      page_token = None
      while True:
        messages, page_token = runner.dataflow_client.list_messages(
            job_id, page_token=page_token, start_time=last_message_time)
        for m in messages:
          message = '%s: %s: %s' % (m.time, m.messageImportance, m.messageText)

          if not last_message_time or m.time > last_message_time:
            last_message_time = m.time
            current_seen_messages = set()

          if message in current_seen_messages:
            # Skip the message if it has already been seen at the current
            # time. This could be the case since the list_messages API is
            # queried starting at last_message_time.
            continue
          else:
            current_seen_messages.add(message)
          # Skip empty messages.
          if m.messageImportance is None:
            continue
          logging.info(message)
          if str(m.messageImportance) == 'JOB_MESSAGE_ERROR':
            if rank_error(m.messageText) >= last_error_rank:
              last_error_rank = rank_error(m.messageText)
              last_error_msg = m.messageText
        if not page_token:
          break

      if duration:
        passed_secs = time.time() - start_secs
        if passed_secs > duration_secs:
          logging.warning('Timing out on waiting for job %s after %d seconds',
                          job_id, passed_secs)
          break

    result._job = response
    runner.last_error_msg = last_error_msg

  @staticmethod
  def group_by_key_input_visitor():
    # Imported here to avoid circular dependencies.
    from apache_beam.pipeline import PipelineVisitor

    class GroupByKeyInputVisitor(PipelineVisitor):
      """A visitor that replaces `Any` element type for input `PCollection` of
      a `GroupByKey` or `_GroupByKeyOnly` with a `KV` type.

      TODO(BEAM-115): Once Python SDk is compatible with the new Runner API,
      we could directly replace the coder instead of mutating the element type.
      """

      def enter_composite_transform(self, transform_node):
        self.visit_transform(transform_node)

      def visit_transform(self, transform_node):
        # Imported here to avoid circular dependencies.
        # pylint: disable=wrong-import-order, wrong-import-position
        from apache_beam.transforms.core import GroupByKey, _GroupByKeyOnly
        if isinstance(transform_node.transform, (GroupByKey, _GroupByKeyOnly)):
          pcoll = transform_node.inputs[0]
          pcoll.element_type = typehints.coerce_to_kv_type(
              pcoll.element_type, transform_node.full_label)
          key_type, value_type = pcoll.element_type.tuple_types
          if transform_node.outputs:
            from apache_beam.runners.portability.fn_api_runner_transforms import \
              only_element
            key = (
                None if None in transform_node.outputs.keys()
                else only_element(transform_node.outputs.keys()))
            transform_node.outputs[key].element_type = typehints.KV[
                key_type, typehints.Iterable[value_type]]

    return GroupByKeyInputVisitor()

  @staticmethod
  def _set_pdone_visitor(pipeline):
    # Imported here to avoid circular dependencies.
    from apache_beam.pipeline import PipelineVisitor

    class SetPDoneVisitor(PipelineVisitor):

      def __init__(self, pipeline):
        self._pipeline = pipeline

      @staticmethod
      def _maybe_fix_output(transform_node, pipeline):
        if not transform_node.outputs:
          pval = pvalue.PDone(pipeline)
          pval.producer = transform_node
          transform_node.outputs = {None: pval}

      def enter_composite_transform(self, transform_node):
        SetPDoneVisitor._maybe_fix_output(transform_node, self._pipeline)

      def visit_transform(self, transform_node):
        SetPDoneVisitor._maybe_fix_output(transform_node, self._pipeline)

    return SetPDoneVisitor(pipeline)

  @staticmethod
  def side_input_visitor():
    # Imported here to avoid circular dependencies.
    # pylint: disable=wrong-import-order, wrong-import-position
    from apache_beam.pipeline import PipelineVisitor
    from apache_beam.transforms.core import ParDo

    class SideInputVisitor(PipelineVisitor):
      """Ensures input `PCollection` used as a side inputs has a `KV` type.

      TODO(BEAM-115): Once Python SDK is compatible with the new Runner API,
      we could directly replace the coder instead of mutating the element type.
      """
      def visit_transform(self, transform_node):
        if isinstance(transform_node.transform, ParDo):
          new_side_inputs = []
          for ix, side_input in enumerate(transform_node.side_inputs):
            access_pattern = side_input._side_input_data().access_pattern
            if access_pattern == common_urns.side_inputs.ITERABLE.urn:
              # Add a map to ('', value) as Dataflow currently only handles
              # keyed side inputs.
              pipeline = side_input.pvalue.pipeline
              new_side_input = _DataflowIterableSideInput(side_input)
              new_side_input.pvalue = beam.pvalue.PCollection(
                  pipeline,
                  element_type=typehints.KV[
                      bytes, side_input.pvalue.element_type],
                  is_bounded=side_input.pvalue.is_bounded)
              parent = transform_node.parent or pipeline._root_transform()
              map_to_void_key = beam.pipeline.AppliedPTransform(
                  pipeline,
                  beam.Map(lambda x: (b'', x)),
                  transform_node.full_label + '/MapToVoidKey%s' % ix,
                  (side_input.pvalue,))
              new_side_input.pvalue.producer = map_to_void_key
              map_to_void_key.add_output(new_side_input.pvalue)
              parent.add_part(map_to_void_key)
            elif access_pattern == common_urns.side_inputs.MULTIMAP.urn:
              # Ensure the input coder is a KV coder and patch up the
              # access pattern to appease Dataflow.
              side_input.pvalue.element_type = typehints.coerce_to_kv_type(
                  side_input.pvalue.element_type, transform_node.full_label)
              new_side_input = _DataflowMultimapSideInput(side_input)
            else:
              raise ValueError(
                  'Unsupported access pattern for %r: %r' %
                  (transform_node.full_label, access_pattern))
            new_side_inputs.append(new_side_input)
          transform_node.side_inputs = new_side_inputs
          transform_node.transform.side_inputs = new_side_inputs

    return SideInputVisitor()

  @staticmethod
  def flatten_input_visitor():
    # Imported here to avoid circular dependencies.
    from apache_beam.pipeline import PipelineVisitor

    class FlattenInputVisitor(PipelineVisitor):
      """A visitor that replaces the element type for input ``PCollections``s of
       a ``Flatten`` transform with that of the output ``PCollection``.
      """

      def visit_transform(self, transform_node):
        # Imported here to avoid circular dependencies.
        # pylint: disable=wrong-import-order, wrong-import-position
        from apache_beam import Flatten
        if isinstance(transform_node.transform, Flatten):
          output_pcoll = transform_node.outputs[None]
          for input_pcoll in transform_node.inputs:
            input_pcoll.element_type = output_pcoll.element_type

    return FlattenInputVisitor()

  def run_pipeline(self, pipeline, options):
    """Remotely executes entire pipeline or parts reachable from node."""
    # Label goog-dataflow-notebook if pipeline is initiated from interactive
    # runner.
    if pipeline.interactive:
      notebook_version = ('goog-dataflow-notebook=' +
                          beam.version.__version__.replace('.', '_'))
      if options.view_as(GoogleCloudOptions).labels:
        options.view_as(GoogleCloudOptions).labels.append(notebook_version)
      else:
        options.view_as(GoogleCloudOptions).labels = [notebook_version]

    # Import here to avoid adding the dependency for local running scenarios.
    try:
      # pylint: disable=wrong-import-order, wrong-import-position
      from apache_beam.runners.dataflow.internal import apiclient
    except ImportError:
      raise ImportError(
          'Google Cloud Dataflow runner not available, '
          'please install apache_beam[gcp]')

    # Convert all side inputs into a form acceptable to Dataflow.
    if apiclient._use_fnapi(options):
      pipeline.visit(self.side_input_visitor())

    # Performing configured PTransform overrides.  Note that this is currently
    # done before Runner API serialization, since the new proto needs to contain
    # any added PTransforms.
    pipeline.replace_all(DataflowRunner._PTRANSFORM_OVERRIDES)
    if apiclient._use_sdf_bounded_source(options):
      pipeline.replace_all(DataflowRunner._SDF_PTRANSFORM_OVERRIDES)

    use_fnapi = apiclient._use_fnapi(options)
    from apache_beam.portability.api import beam_runner_api_pb2
    default_environment = beam_runner_api_pb2.Environment(
        urn=common_urns.environments.DOCKER.urn,
        payload=beam_runner_api_pb2.DockerPayload(
            container_image=apiclient.get_container_image_from_options(options)
            ).SerializeToString())

    # Snapshot the pipeline in a portable proto.
    self.proto_pipeline, self.proto_context = pipeline.to_runner_api(
        return_context=True, default_environment=default_environment)

    if use_fnapi:
      # Cross language transform require using a pipeline object constructed
      # from the full pipeline proto to make sure that expanded version of
      # external transforms are reflected in the Pipeline job graph.
      from apache_beam import Pipeline
      pipeline = Pipeline.from_runner_api(
          self.proto_pipeline, pipeline.runner, options,
          allow_proto_holders=True)

      # Pipelines generated from proto do not have output set to PDone set for
      # leaf elements.
      pipeline.visit(self._set_pdone_visitor(pipeline))

      # We need to generate a new context that maps to the new pipeline object.
      self.proto_pipeline, self.proto_context = pipeline.to_runner_api(
          return_context=True, default_environment=default_environment)

    # Add setup_options for all the BeamPlugin imports
    setup_options = options.view_as(SetupOptions)
    plugins = BeamPlugin.get_all_plugin_paths()
    if setup_options.beam_plugins is not None:
      plugins = list(set(plugins + setup_options.beam_plugins))
    setup_options.beam_plugins = plugins

    # Elevate "min_cpu_platform" to pipeline option, but using the existing
    # experiment.
    debug_options = options.view_as(DebugOptions)
    worker_options = options.view_as(WorkerOptions)
    if worker_options.min_cpu_platform:
      debug_options.add_experiment('min_cpu_platform=' +
                                   worker_options.min_cpu_platform)

    # Elevate "enable_streaming_engine" to pipeline option, but using the
    # existing experiment.
    google_cloud_options = options.view_as(GoogleCloudOptions)
    if google_cloud_options.enable_streaming_engine:
      debug_options.add_experiment("enable_windmill_service")
      debug_options.add_experiment("enable_streaming_engine")
    else:
      if (debug_options.lookup_experiment("enable_windmill_service") or
          debug_options.lookup_experiment("enable_streaming_engine")):
        raise ValueError("""Streaming engine both disabled and enabled:
        enable_streaming_engine flag is not set, but enable_windmill_service
        and/or enable_streaming_engine experiments are present.
        It is recommended you only set the enable_streaming_engine flag.""")

    dataflow_worker_jar = getattr(worker_options, 'dataflow_worker_jar', None)
    if dataflow_worker_jar is not None:
      if not apiclient._use_fnapi(options):
        logging.warning(
            'Typical end users should not use this worker jar feature. '
            'It can only be used when FnAPI is enabled.')
      else:
        debug_options.add_experiment('use_staged_dataflow_worker_jar')

    # Make Dataflow workers use FastAvro on Python 3 unless use_avro experiment
    # is set. Note that use_avro is only interpreted by the Dataflow runner
    # at job submission and is not interpreted by Dataflow service or workers,
    # which by default use avro library unless use_fastavro experiment is set.
    if sys.version_info[0] > 2 and (
        not debug_options.lookup_experiment('use_avro')):
      debug_options.add_experiment('use_fastavro')

    self.job = apiclient.Job(options, self.proto_pipeline)

    # Dataflow runner requires a KV type for GBK inputs, hence we enforce that
    # here.
    pipeline.visit(self.group_by_key_input_visitor())

    # Dataflow runner requires output type of the Flatten to be the same as the
    # inputs, hence we enforce that here.
    pipeline.visit(self.flatten_input_visitor())

    # The superclass's run will trigger a traversal of all reachable nodes.
    super(DataflowRunner, self).run_pipeline(pipeline, options)

    test_options = options.view_as(TestOptions)
    # If it is a dry run, return without submitting the job.
    if test_options.dry_run:
      return None

    # Get a Dataflow API client and set its options
    self.dataflow_client = apiclient.DataflowApplicationClient(options)

    # Create the job description and send a request to the service. The result
    # can be None if there is no need to send a request to the service (e.g.
    # template creation). If a request was sent and failed then the call will
    # raise an exception.
    result = DataflowPipelineResult(
        self.dataflow_client.create_job(self.job), self)

    # TODO(BEAM-4274): Circular import runners-metrics. Requires refactoring.
    from apache_beam.runners.dataflow.dataflow_metrics import DataflowMetrics
    self._metrics = DataflowMetrics(self.dataflow_client, result, self.job)
    result.metric_results = self._metrics
    return result

  def _get_typehint_based_encoding(self, typehint, window_coder, use_fnapi):
    """Returns an encoding based on a typehint object."""
    return self._get_cloud_encoding(
        self._get_coder(typehint, window_coder=window_coder), use_fnapi)

  @staticmethod
  def _get_coder(typehint, window_coder):
    """Returns a coder based on a typehint object."""
    if window_coder:
      return coders.WindowedValueCoder(
          coders.registry.get_coder(typehint),
          window_coder=window_coder)
    return coders.registry.get_coder(typehint)

  def _get_cloud_encoding(self, coder, use_fnapi):
    """Returns an encoding based on a coder object."""
    if not isinstance(coder, coders.Coder):
      raise TypeError('Coder object must inherit from coders.Coder: %s.' %
                      str(coder))
    return coder.as_cloud_object(self.proto_context
                                 .coders if use_fnapi else None)

  def _get_side_input_encoding(self, input_encoding):
    """Returns an encoding for the output of a view transform.

    Args:
      input_encoding: encoding of current transform's input. Side inputs need
        this because the service will check that input and output types match.

    Returns:
      An encoding that matches the output and input encoding. This is essential
      for the View transforms introduced to produce side inputs to a ParDo.
    """
    return {
        '@type': 'kind:stream',
        'component_encodings': [input_encoding],
        'is_stream_like': {
            'value': True
        },
    }

  def _get_encoded_output_coder(self, transform_node, window_value=True):
    """Returns the cloud encoding of the coder for the output of a transform."""
    from apache_beam.runners.portability.fn_api_runner_transforms import \
      only_element
    if len(transform_node.outputs) == 1:
      output_tag = only_element(transform_node.outputs.keys())
      # TODO(robertwb): Handle type hints for multi-output transforms.
      element_type = transform_node.outputs[output_tag].element_type
    else:
      # TODO(silviuc): Remove this branch (and assert) when typehints are
      # propagated everywhere. Returning an 'Any' as type hint will trigger
      # usage of the fallback coder (i.e., cPickler).
      element_type = typehints.Any
    if window_value:
      # All outputs have the same windowing. So getting the coder from an
      # arbitrary window is fine.
      output_tag = next(iter(transform_node.outputs.keys()))
      window_coder = (
          transform_node.outputs[
              output_tag].windowing.windowfn.get_window_coder())
    else:
      window_coder = None
    from apache_beam.runners.dataflow.internal import apiclient
    use_fnapi = apiclient._use_fnapi(
        list(transform_node.outputs.values())[0].pipeline._options)
    return self._get_typehint_based_encoding(element_type, window_coder,
                                             use_fnapi)

  def _add_step(self, step_kind, step_label, transform_node, side_tags=()):
    """Creates a Step object and adds it to the cache."""
    # Import here to avoid adding the dependency for local running scenarios.
    # pylint: disable=wrong-import-order, wrong-import-position
    from apache_beam.runners.dataflow.internal import apiclient
    step = apiclient.Step(step_kind, self._get_unique_step_name())
    self.job.proto.steps.append(step.proto)
    step.add_property(PropertyNames.USER_NAME, step_label)
    # Cache the node/step association for the main output of the transform node.

    # Main output key of external transforms can be ambiguous, so we only tag if
    # there's only one tag instead of None.
    from apache_beam.runners.portability.fn_api_runner_transforms import only_element
    output_tag = (only_element(transform_node.outputs.keys())
                  if len(transform_node.outputs.keys()) == 1 else None)

    self._cache.cache_output(transform_node, output_tag, step)
    # If side_tags is not () then this is a multi-output transform node and we
    # need to cache the (node, tag, step) for each of the tags used to access
    # the outputs. This is essential because the keys used to search in the
    # cache always contain the tag.
    for tag in side_tags:
      self._cache.cache_output(transform_node, tag, step)

    # Finally, we add the display data items to the pipeline step.
    # If the transform contains no display data then an empty list is added.
    step.add_property(
        PropertyNames.DISPLAY_DATA,
        [item.get_dict() for item in
         DisplayData.create_from(transform_node.transform).items])

    return step

  def _add_singleton_step(
      self, label, full_label, tag, input_step, windowing_strategy):
    """Creates a CollectionToSingleton step used to handle ParDo side inputs."""
    # Import here to avoid adding the dependency for local running scenarios.
    from apache_beam.runners.dataflow.internal import apiclient
    step = apiclient.Step(TransformNames.COLLECTION_TO_SINGLETON, label)
    self.job.proto.steps.append(step.proto)
    step.add_property(PropertyNames.USER_NAME, full_label)
    step.add_property(
        PropertyNames.PARALLEL_INPUT,
        {'@type': 'OutputReference',
         PropertyNames.STEP_NAME: input_step.proto.name,
         PropertyNames.OUTPUT_NAME: input_step.get_output(tag)})
    step.encoding = self._get_side_input_encoding(input_step.encoding)
    step.add_property(
        PropertyNames.OUTPUT_INFO,
        [{PropertyNames.USER_NAME: (
            '%s.%s' % (full_label, PropertyNames.OUTPUT)),
          PropertyNames.ENCODING: step.encoding,
          PropertyNames.OUTPUT_NAME: PropertyNames.OUT}])
    step.add_property(
        PropertyNames.WINDOWING_STRATEGY,
        self.serialize_windowing_strategy(windowing_strategy))
    return step

  def run_Impulse(self, transform_node, options):
    standard_options = options.view_as(StandardOptions)
    step = self._add_step(
        TransformNames.READ, transform_node.full_label, transform_node)
    if standard_options.streaming:
      step.add_property(PropertyNames.FORMAT, 'pubsub')
      step.add_property(PropertyNames.PUBSUB_SUBSCRIPTION, '_starting_signal/')
    else:
      step.add_property(PropertyNames.FORMAT, 'impulse')
      encoded_impulse_element = coders.WindowedValueCoder(
          coders.BytesCoder(),
          coders.coders.GlobalWindowCoder()).get_impl().encode_nested(
              window.GlobalWindows.windowed_value(b''))

      from apache_beam.runners.dataflow.internal import apiclient
      if apiclient._use_fnapi(options):
        encoded_impulse_as_str = self.byte_array_to_json_string(
            encoded_impulse_element)
      else:
        encoded_impulse_as_str = base64.b64encode(
            encoded_impulse_element).decode('ascii')
      step.add_property(PropertyNames.IMPULSE_ELEMENT,
                        encoded_impulse_as_str)

    step.encoding = self._get_encoded_output_coder(transform_node)
    step.add_property(
        PropertyNames.OUTPUT_INFO,
        [{PropertyNames.USER_NAME: (
            '%s.%s' % (
                transform_node.full_label, PropertyNames.OUT)),
          PropertyNames.ENCODING: step.encoding,
          PropertyNames.OUTPUT_NAME: PropertyNames.OUT}])

  def run_Flatten(self, transform_node, options):
    step = self._add_step(TransformNames.FLATTEN,
                          transform_node.full_label, transform_node)
    inputs = []
    for one_input in transform_node.inputs:
      input_step = self._cache.get_pvalue(one_input)
      inputs.append(
          {'@type': 'OutputReference',
           PropertyNames.STEP_NAME: input_step.proto.name,
           PropertyNames.OUTPUT_NAME: input_step.get_output(one_input.tag)})
    step.add_property(PropertyNames.INPUTS, inputs)
    step.encoding = self._get_encoded_output_coder(transform_node)
    step.add_property(
        PropertyNames.OUTPUT_INFO,
        [{PropertyNames.USER_NAME: (
            '%s.%s' % (transform_node.full_label, PropertyNames.OUT)),
          PropertyNames.ENCODING: step.encoding,
          PropertyNames.OUTPUT_NAME: PropertyNames.OUT}])

  def apply_WriteToBigQuery(self, transform, pcoll, options):
    # Make sure this is the WriteToBigQuery class that we expected, and that
    # users did not specifically request the new BQ sink by passing experiment
    # flag.

    # TODO(BEAM-6928): Remove this function for release 2.14.0.
    experiments = options.view_as(DebugOptions).experiments or []
    if (not isinstance(transform, beam.io.WriteToBigQuery)
        or 'use_beam_bq_sink' in experiments):
      return self.apply_PTransform(transform, pcoll, options)
    if transform.schema == beam.io.gcp.bigquery.SCHEMA_AUTODETECT:
      raise RuntimeError(
          'Schema auto-detection is not supported on the native sink')
    standard_options = options.view_as(StandardOptions)
    if standard_options.streaming:
      if (transform.write_disposition ==
          beam.io.BigQueryDisposition.WRITE_TRUNCATE):
        raise RuntimeError('Can not use write truncation mode in streaming')
      return self.apply_PTransform(transform, pcoll, options)
    else:
      from apache_beam.io.gcp.bigquery_tools import parse_table_schema_from_json
      schema = None
      if transform.schema:
        schema = parse_table_schema_from_json(json.dumps(transform.schema))
      return pcoll  | 'WriteToBigQuery' >> beam.io.Write(
          beam.io.BigQuerySink(
              transform.table_reference.tableId,
              transform.table_reference.datasetId,
              transform.table_reference.projectId,
              schema,
              transform.create_disposition,
              transform.write_disposition,
              kms_key=transform.kms_key))

  def apply_GroupByKey(self, transform, pcoll, options):
    # Infer coder of parent.
    #
    # TODO(ccy): make Coder inference and checking less specialized and more
    # comprehensive.
    parent = pcoll.producer
    if parent:
      coder = parent.transform._infer_output_coder()  # pylint: disable=protected-access
    if not coder:
      coder = self._get_coder(pcoll.element_type or typehints.Any, None)
    if not coder.is_kv_coder():
      raise ValueError(('Coder for the GroupByKey operation "%s" is not a '
                        'key-value coder: %s.') % (transform.label,
                                                   coder))
    # TODO(robertwb): Update the coder itself if it changed.
    coders.registry.verify_deterministic(
        coder.key_coder(), 'GroupByKey operation "%s"' % transform.label)

    return pvalue.PCollection.from_(pcoll)

  def run_GroupByKey(self, transform_node, options):
    input_tag = transform_node.inputs[0].tag
    input_step = self._cache.get_pvalue(transform_node.inputs[0])
    step = self._add_step(
        TransformNames.GROUP, transform_node.full_label, transform_node)
    step.add_property(
        PropertyNames.PARALLEL_INPUT,
        {'@type': 'OutputReference',
         PropertyNames.STEP_NAME: input_step.proto.name,
         PropertyNames.OUTPUT_NAME: input_step.get_output(input_tag)})
    step.encoding = self._get_encoded_output_coder(transform_node)
    step.add_property(
        PropertyNames.OUTPUT_INFO,
        [{PropertyNames.USER_NAME: (
            '%s.%s' % (transform_node.full_label, PropertyNames.OUT)),
          PropertyNames.ENCODING: step.encoding,
          PropertyNames.OUTPUT_NAME: PropertyNames.OUT}])
    windowing = transform_node.transform.get_windowing(
        transform_node.inputs)
    step.add_property(
        PropertyNames.SERIALIZED_FN,
        self.serialize_windowing_strategy(windowing))

  def run_RunnerAPIPTransformHolder(self, transform_node, options):
    """Adding Dataflow runner job description for transform holder objects.

    These holder transform objects are generated for some of the transforms that
    become available after a cross-language transform expansion, usually if the
    corresponding transform object cannot be generated in Python SDK (for
    example, a python `ParDo` transform cannot be generated without a serialized
    Python `DoFn` object).
    """
    urn = transform_node.transform.proto().urn
    assert urn
    # TODO(chamikara): support other transforms that requires holder objects in
    #  Python SDk.
    if common_urns.primitives.PAR_DO.urn == urn:
      self.run_ParDo(transform_node, options)
    else:
      NotImplementedError(urn)

  def run_ParDo(self, transform_node, options):
    transform = transform_node.transform
    input_tag = transform_node.inputs[0].tag
    input_step = self._cache.get_pvalue(transform_node.inputs[0])

    # Attach side inputs.
    si_dict = {}
    si_labels = {}
    full_label_counts = defaultdict(int)
    lookup_label = lambda side_pval: si_labels[side_pval]
    named_inputs = transform_node.named_inputs()
    label_renames = {}
    for ix, side_pval in enumerate(transform_node.side_inputs):
      assert isinstance(side_pval, AsSideInput)
      step_name = 'SideInput-' + self._get_unique_step_name()
      si_label = 'side%d-%s' % (ix, transform_node.full_label)
      old_label = 'side%d' % ix
      label_renames[old_label] = si_label
      assert old_label in named_inputs
      pcollection_label = '%s.%s' % (
          side_pval.pvalue.producer.full_label.split('/')[-1],
          side_pval.pvalue.tag if side_pval.pvalue.tag else 'out')
      si_full_label = '%s/%s(%s.%s)' % (transform_node.full_label,
                                        side_pval.__class__.__name__,
                                        pcollection_label,
                                        full_label_counts[pcollection_label])

      # Count the number of times the same PCollection is a side input
      # to the same ParDo.
      full_label_counts[pcollection_label] += 1

      self._add_singleton_step(
          step_name, si_full_label, side_pval.pvalue.tag,
          self._cache.get_pvalue(side_pval.pvalue),
          side_pval.pvalue.windowing)
      si_dict[si_label] = {
          '@type': 'OutputReference',
          PropertyNames.STEP_NAME: step_name,
          PropertyNames.OUTPUT_NAME: PropertyNames.OUT}
      si_labels[side_pval] = si_label

    # Now create the step for the ParDo transform being handled.
    transform_name = transform_node.full_label.rsplit('/', 1)[-1]
    step = self._add_step(
        TransformNames.DO,
        transform_node.full_label + (
            '/{}'.format(transform_name)
            if transform_node.side_inputs else ''),
        transform_node,
        transform_node.transform.output_tags)
    # Import here to avoid adding the dependency for local running scenarios.
    # pylint: disable=wrong-import-order, wrong-import-position
    from apache_beam.runners.dataflow.internal import apiclient
    transform_proto = self.proto_context.transforms.get_proto(transform_node)
    transform_id = self.proto_context.transforms.get_id(transform_node)
    use_fnapi = apiclient._use_fnapi(options)
    use_unified_worker = apiclient._use_unified_worker(options)
    # The data transmitted in SERIALIZED_FN is different depending on whether
    # this is a fnapi pipeline or not.
    if (use_fnapi and
        (transform_proto.spec.urn == common_urns.primitives.PAR_DO.urn or
         use_unified_worker)):
      # Patch side input ids to be unique across a given pipeline.
      if (label_renames and
          transform_proto.spec.urn == common_urns.primitives.PAR_DO.urn):
        # Patch PTransform proto.
        for old, new in iteritems(label_renames):
          transform_proto.inputs[new] = transform_proto.inputs[old]
          del transform_proto.inputs[old]

        # Patch ParDo proto.
        proto_type, _ = beam.PTransform._known_urns[transform_proto.spec.urn]
        proto = proto_utils.parse_Bytes(transform_proto.spec.payload,
                                        proto_type)
        for old, new in iteritems(label_renames):
          proto.side_inputs[new].CopyFrom(proto.side_inputs[old])
          del proto.side_inputs[old]
        transform_proto.spec.payload = proto.SerializeToString()
        # We need to update the pipeline proto.
        del self.proto_pipeline.components.transforms[transform_id]
        (self.proto_pipeline.components.transforms[transform_id]
         .CopyFrom(transform_proto))
      serialized_data = transform_id
    else:
      serialized_data = pickler.dumps(
          self._pardo_fn_data(transform_node, lookup_label))
    step.add_property(PropertyNames.SERIALIZED_FN, serialized_data)
    step.add_property(
        PropertyNames.PARALLEL_INPUT,
        {'@type': 'OutputReference',
         PropertyNames.STEP_NAME: input_step.proto.name,
         PropertyNames.OUTPUT_NAME: input_step.get_output(input_tag)})
    # Add side inputs if any.
    step.add_property(PropertyNames.NON_PARALLEL_INPUTS, si_dict)

    # Generate description for the outputs. The output names
    # will be 'out' for main output and 'out_<tag>' for a tagged output.
    # Using 'out' as a tag will not clash with the name for main since it will
    # be transformed into 'out_out' internally.
    outputs = []
    step.encoding = self._get_encoded_output_coder(transform_node)

    all_output_tags = transform_proto.outputs.keys()

    from apache_beam.transforms.core import RunnerAPIPTransformHolder
    external_transform = isinstance(transform, RunnerAPIPTransformHolder)

    # Some external transforms require output tags to not be modified.
    # So we randomly select one of the output tags as the main output and
    # leave others as side outputs. Transform execution should not change
    # dependending on which output tag we choose as the main output here.
    # Also, some SDKs do not work correctly if output tags are modified. So for
    # external transforms, we leave tags unmodified.
    main_output_tag = (
        all_output_tags[0] if external_transform else PropertyNames.OUT)

    # Python SDK uses 'None' as the tag of the main output.
    tag_to_ignore = main_output_tag if external_transform else 'None'
    side_output_tags = set(all_output_tags).difference({tag_to_ignore})

    # Add the main output to the description.
    outputs.append(
        {PropertyNames.USER_NAME: (
            '%s.%s' % (transform_node.full_label, PropertyNames.OUT)),
         PropertyNames.ENCODING: step.encoding,
         PropertyNames.OUTPUT_NAME: main_output_tag})
    for side_tag in side_output_tags:
      # The assumption here is that all outputs will have the same typehint
      # and coder as the main output. This is certainly the case right now
      # but conceivably it could change in the future.
      outputs.append(
          {PropertyNames.USER_NAME: (
              '%s.%s' % (transform_node.full_label, side_tag)),
           PropertyNames.ENCODING: step.encoding,
           PropertyNames.OUTPUT_NAME: (
               side_tag if external_transform
               else '%s_%s' % (PropertyNames.OUT, side_tag))})

    step.add_property(PropertyNames.OUTPUT_INFO, outputs)

    # Add the restriction encoding if we are a splittable DoFn
    # and are using the Fn API on the unified worker.
    restriction_coder = transform.get_restriction_coder()
    if (use_fnapi and use_unified_worker and restriction_coder):
      step.add_property(PropertyNames.RESTRICTION_ENCODING,
                        self._get_cloud_encoding(
                            restriction_coder, use_fnapi))

  @staticmethod
  def _pardo_fn_data(transform_node, get_label):
    transform = transform_node.transform
    si_tags_and_types = [  # pylint: disable=protected-access
        (get_label(side_pval), side_pval.__class__, side_pval._view_options())
        for side_pval in transform_node.side_inputs]
    return (transform.fn, transform.args, transform.kwargs, si_tags_and_types,
            transform_node.inputs[0].windowing)

  def apply_CombineValues(self, transform, pcoll, options):
    return pvalue.PCollection.from_(pcoll)

  def run_CombineValues(self, transform_node, options):
    transform = transform_node.transform
    input_tag = transform_node.inputs[0].tag
    input_step = self._cache.get_pvalue(transform_node.inputs[0])
    step = self._add_step(
        TransformNames.COMBINE, transform_node.full_label, transform_node)

    # The data transmitted in SERIALIZED_FN is different depending on whether
    # this is a fnapi pipeline or not.
    from apache_beam.runners.dataflow.internal import apiclient
    use_fnapi = apiclient._use_fnapi(options)
    if use_fnapi:
      # Fnapi pipelines send the transform ID of the CombineValues transform's
      # parent composite because Dataflow expects the ID of a CombinePerKey
      # transform.
      serialized_data = self.proto_context.transforms.get_id(
          transform_node.parent)
    else:
      # Combiner functions do not take deferred side-inputs (i.e. PValues) and
      # therefore the code to handle extra args/kwargs is simpler than for the
      # DoFn's of the ParDo transform. In the last, empty argument is where
      # side inputs information would go.
      serialized_data = pickler.dumps((transform.fn, transform.args,
                                       transform.kwargs, ()))
    step.add_property(PropertyNames.SERIALIZED_FN, serialized_data)
    step.add_property(
        PropertyNames.PARALLEL_INPUT,
        {'@type': 'OutputReference',
         PropertyNames.STEP_NAME: input_step.proto.name,
         PropertyNames.OUTPUT_NAME: input_step.get_output(input_tag)})
    # Note that the accumulator must not have a WindowedValue encoding, while
    # the output of this step does in fact have a WindowedValue encoding.
    accumulator_encoding = self._get_cloud_encoding(
        transform_node.transform.fn.get_accumulator_coder(), use_fnapi)
    output_encoding = self._get_encoded_output_coder(transform_node)

    step.encoding = output_encoding
    step.add_property(PropertyNames.ENCODING, accumulator_encoding)
    # Generate description for main output 'out.'
    outputs = []
    # Add the main output to the description.
    outputs.append(
        {PropertyNames.USER_NAME: (
            '%s.%s' % (transform_node.full_label, PropertyNames.OUT)),
         PropertyNames.ENCODING: step.encoding,
         PropertyNames.OUTPUT_NAME: PropertyNames.OUT})
    step.add_property(PropertyNames.OUTPUT_INFO, outputs)

  def apply_Read(self, transform, pbegin, options):
    if hasattr(transform.source, 'format'):
      # Consider native Read to be a primitive for dataflow.
      return beam.pvalue.PCollection.from_(pbegin)
    else:
      debug_options = options.view_as(DebugOptions)
      if (
          debug_options.experiments and
          'beam_fn_api' in debug_options.experiments
      ):
        # Expand according to FnAPI primitives.
        return self.apply_PTransform(transform, pbegin, options)
      else:
        # Custom Read is also a primitive for non-FnAPI on dataflow.
        return beam.pvalue.PCollection.from_(pbegin)

  def run_Read(self, transform_node, options):
    transform = transform_node.transform
    step = self._add_step(
        TransformNames.READ, transform_node.full_label, transform_node)
    # TODO(mairbek): refactor if-else tree to use registerable functions.
    # Initialize the source specific properties.

    standard_options = options.view_as(StandardOptions)
    if not hasattr(transform.source, 'format'):
      # If a format is not set, we assume the source to be a custom source.
      source_dict = {}

      source_dict['spec'] = {
          '@type': names.SOURCE_TYPE,
          names.SERIALIZED_SOURCE_KEY: pickler.dumps(transform.source)
      }

      try:
        source_dict['metadata'] = {
            'estimated_size_bytes': json_value.get_typed_value_descriptor(
                transform.source.estimate_size())
        }
      except error.RuntimeValueProviderError:
        # Size estimation is best effort, and this error is by value provider.
        logging.info(
            'Could not estimate size of source %r due to ' + \
            'RuntimeValueProviderError', transform.source)
      except Exception:  # pylint: disable=broad-except
        # Size estimation is best effort. So we log the error and continue.
        logging.info(
            'Could not estimate size of source %r due to an exception: %s',
            transform.source, traceback.format_exc())

      step.add_property(PropertyNames.SOURCE_STEP_INPUT,
                        source_dict)
    elif transform.source.format == 'text':
      step.add_property(PropertyNames.FILE_PATTERN, transform.source.path)
    elif transform.source.format == 'bigquery':
      if standard_options.streaming:
        raise ValueError('BigQuery source is not currently available for use '
                         'in streaming pipelines.')
      step.add_property(PropertyNames.BIGQUERY_EXPORT_FORMAT, 'FORMAT_AVRO')
      # TODO(silviuc): Add table validation if transform.source.validate.
      if transform.source.table_reference is not None:
        step.add_property(PropertyNames.BIGQUERY_DATASET,
                          transform.source.table_reference.datasetId)
        step.add_property(PropertyNames.BIGQUERY_TABLE,
                          transform.source.table_reference.tableId)
        # If project owning the table was not specified then the project owning
        # the workflow (current project) will be used.
        if transform.source.table_reference.projectId is not None:
          step.add_property(PropertyNames.BIGQUERY_PROJECT,
                            transform.source.table_reference.projectId)
      elif transform.source.query is not None:
        step.add_property(PropertyNames.BIGQUERY_QUERY, transform.source.query)
        step.add_property(PropertyNames.BIGQUERY_USE_LEGACY_SQL,
                          transform.source.use_legacy_sql)
        step.add_property(PropertyNames.BIGQUERY_FLATTEN_RESULTS,
                          transform.source.flatten_results)
      else:
        raise ValueError('BigQuery source %r must specify either a table or'
                         ' a query' % transform.source)
      if transform.source.kms_key is not None:
        step.add_property(
            PropertyNames.BIGQUERY_KMS_KEY, transform.source.kms_key)
    elif transform.source.format == 'pubsub':
      if not standard_options.streaming:
        raise ValueError('Cloud Pub/Sub is currently available for use '
                         'only in streaming pipelines.')
      # Only one of topic or subscription should be set.
      if transform.source.full_subscription:
        step.add_property(PropertyNames.PUBSUB_SUBSCRIPTION,
                          transform.source.full_subscription)
      elif transform.source.full_topic:
        step.add_property(PropertyNames.PUBSUB_TOPIC,
                          transform.source.full_topic)
      if transform.source.id_label:
        step.add_property(PropertyNames.PUBSUB_ID_LABEL,
                          transform.source.id_label)
      if transform.source.with_attributes:
        # Setting this property signals Dataflow runner to return full
        # PubsubMessages instead of just the data part of the payload.
        step.add_property(PropertyNames.PUBSUB_SERIALIZED_ATTRIBUTES_FN, '')
      if transform.source.timestamp_attribute is not None:
        step.add_property(PropertyNames.PUBSUB_TIMESTAMP_ATTRIBUTE,
                          transform.source.timestamp_attribute)
    else:
      raise ValueError(
          'Source %r has unexpected format %s.' % (
              transform.source, transform.source.format))

    if not hasattr(transform.source, 'format'):
      step.add_property(PropertyNames.FORMAT, names.SOURCE_FORMAT)
    else:
      step.add_property(PropertyNames.FORMAT, transform.source.format)

    # Wrap coder in WindowedValueCoder: this is necessary as the encoding of a
    # step should be the type of value outputted by each step.  Read steps
    # automatically wrap output values in a WindowedValue wrapper, if necessary.
    # This is also necessary for proper encoding for size estimation.
    # Using a GlobalWindowCoder as a place holder instead of the default
    # PickleCoder because GlobalWindowCoder is known coder.
    # TODO(robertwb): Query the collection for the windowfn to extract the
    # correct coder.
    coder = coders.WindowedValueCoder(
        coders.registry.get_coder(transform_node.outputs[None].element_type),
        coders.coders.GlobalWindowCoder())

    from apache_beam.runners.dataflow.internal import apiclient
    use_fnapi = apiclient._use_fnapi(options)
    step.encoding = self._get_cloud_encoding(coder, use_fnapi)
    step.add_property(
        PropertyNames.OUTPUT_INFO,
        [{PropertyNames.USER_NAME: (
            '%s.%s' % (transform_node.full_label, PropertyNames.OUT)),
          PropertyNames.ENCODING: step.encoding,
          PropertyNames.OUTPUT_NAME: PropertyNames.OUT}])

  def run__NativeWrite(self, transform_node, options):
    transform = transform_node.transform
    input_tag = transform_node.inputs[0].tag
    input_step = self._cache.get_pvalue(transform_node.inputs[0])
    step = self._add_step(
        TransformNames.WRITE, transform_node.full_label, transform_node)
    # TODO(mairbek): refactor if-else tree to use registerable functions.
    # Initialize the sink specific properties.
    if transform.sink.format == 'text':
      # Note that it is important to use typed properties (@type/value dicts)
      # for non-string properties and also for empty strings. For example,
      # in the code below the num_shards must have type and also
      # file_name_suffix and shard_name_template (could be empty strings).
      step.add_property(
          PropertyNames.FILE_NAME_PREFIX, transform.sink.file_name_prefix,
          with_type=True)
      step.add_property(
          PropertyNames.FILE_NAME_SUFFIX, transform.sink.file_name_suffix,
          with_type=True)
      step.add_property(
          PropertyNames.SHARD_NAME_TEMPLATE, transform.sink.shard_name_template,
          with_type=True)
      if transform.sink.num_shards > 0:
        step.add_property(
            PropertyNames.NUM_SHARDS, transform.sink.num_shards, with_type=True)
      # TODO(silviuc): Implement sink validation.
      step.add_property(PropertyNames.VALIDATE_SINK, False, with_type=True)
    elif transform.sink.format == 'bigquery':
      # TODO(silviuc): Add table validation if transform.sink.validate.
      step.add_property(PropertyNames.BIGQUERY_DATASET,
                        transform.sink.table_reference.datasetId)
      step.add_property(PropertyNames.BIGQUERY_TABLE,
                        transform.sink.table_reference.tableId)
      # If project owning the table was not specified then the project owning
      # the workflow (current project) will be used.
      if transform.sink.table_reference.projectId is not None:
        step.add_property(PropertyNames.BIGQUERY_PROJECT,
                          transform.sink.table_reference.projectId)
      step.add_property(PropertyNames.BIGQUERY_CREATE_DISPOSITION,
                        transform.sink.create_disposition)
      step.add_property(PropertyNames.BIGQUERY_WRITE_DISPOSITION,
                        transform.sink.write_disposition)
      if transform.sink.table_schema is not None:
        step.add_property(
            PropertyNames.BIGQUERY_SCHEMA, transform.sink.schema_as_json())
      if transform.sink.kms_key is not None:
        step.add_property(
            PropertyNames.BIGQUERY_KMS_KEY, transform.sink.kms_key)
    elif transform.sink.format == 'pubsub':
      standard_options = options.view_as(StandardOptions)
      if not standard_options.streaming:
        raise ValueError('Cloud Pub/Sub is currently available for use '
                         'only in streaming pipelines.')
      step.add_property(PropertyNames.PUBSUB_TOPIC, transform.sink.full_topic)
      if transform.sink.id_label:
        step.add_property(PropertyNames.PUBSUB_ID_LABEL,
                          transform.sink.id_label)
      if transform.sink.with_attributes:
        # Setting this property signals Dataflow runner that the PCollection
        # contains PubsubMessage objects instead of just raw data.
        step.add_property(PropertyNames.PUBSUB_SERIALIZED_ATTRIBUTES_FN, '')
      if transform.sink.timestamp_attribute is not None:
        step.add_property(PropertyNames.PUBSUB_TIMESTAMP_ATTRIBUTE,
                          transform.sink.timestamp_attribute)
    else:
      raise ValueError(
          'Sink %r has unexpected format %s.' % (
              transform.sink, transform.sink.format))
    step.add_property(PropertyNames.FORMAT, transform.sink.format)

    # Wrap coder in WindowedValueCoder: this is necessary for proper encoding
    # for size estimation. Using a GlobalWindowCoder as a place holder instead
    # of the default PickleCoder because GlobalWindowCoder is known coder.
    # TODO(robertwb): Query the collection for the windowfn to extract the
    # correct coder.
    coder = coders.WindowedValueCoder(transform.sink.coder,
                                      coders.coders.GlobalWindowCoder())
    from apache_beam.runners.dataflow.internal import apiclient
    use_fnapi = apiclient._use_fnapi(options)
    step.encoding = self._get_cloud_encoding(coder, use_fnapi)
    step.add_property(PropertyNames.ENCODING, step.encoding)
    step.add_property(
        PropertyNames.PARALLEL_INPUT,
        {'@type': 'OutputReference',
         PropertyNames.STEP_NAME: input_step.proto.name,
         PropertyNames.OUTPUT_NAME: input_step.get_output(input_tag)})

  @classmethod
  def serialize_windowing_strategy(cls, windowing):
    from apache_beam.runners import pipeline_context
    from apache_beam.portability.api import beam_runner_api_pb2
    context = pipeline_context.PipelineContext()
    windowing_proto = windowing.to_runner_api(context)
    return cls.byte_array_to_json_string(
        beam_runner_api_pb2.MessageWithComponents(
            components=context.to_runner_api(),
            windowing_strategy=windowing_proto).SerializeToString())

  @classmethod
  def deserialize_windowing_strategy(cls, serialized_data):
    # Imported here to avoid circular dependencies.
    # pylint: disable=wrong-import-order, wrong-import-position
    from apache_beam.runners import pipeline_context
    from apache_beam.portability.api import beam_runner_api_pb2
    from apache_beam.transforms.core import Windowing
    proto = beam_runner_api_pb2.MessageWithComponents()
    proto.ParseFromString(cls.json_string_to_byte_array(serialized_data))
    return Windowing.from_runner_api(
        proto.windowing_strategy,
        pipeline_context.PipelineContext(proto.components))

  @staticmethod
  def byte_array_to_json_string(raw_bytes):
    """Implements org.apache.beam.sdk.util.StringUtils.byteArrayToJsonString."""
    return quote(raw_bytes)

  @staticmethod
  def json_string_to_byte_array(encoded_string):
    """Implements org.apache.beam.sdk.util.StringUtils.jsonStringToByteArray."""
    return unquote_to_bytes(encoded_string)


class _DataflowSideInput(beam.pvalue.AsSideInput):
  """Wraps a side input as a dataflow-compatible side input."""

  def _view_options(self):
    return {
        'data': self._data,
    }

  def _side_input_data(self):
    return self._data


class _DataflowIterableSideInput(_DataflowSideInput):
  """Wraps an iterable side input as dataflow-compatible side input."""

  def __init__(self, iterable_side_input):
    # pylint: disable=protected-access
    side_input_data = iterable_side_input._side_input_data()
    assert (
        side_input_data.access_pattern == common_urns.side_inputs.ITERABLE.urn)
    iterable_view_fn = side_input_data.view_fn
    self._data = beam.pvalue.SideInputData(
        common_urns.side_inputs.MULTIMAP.urn,
        side_input_data.window_mapping_fn,
        lambda multimap: iterable_view_fn(multimap[b'']))


class _DataflowMultimapSideInput(_DataflowSideInput):
  """Wraps a multimap side input as dataflow-compatible side input."""

  def __init__(self, side_input):
    # pylint: disable=protected-access
    self.pvalue = side_input.pvalue
    side_input_data = side_input._side_input_data()
    assert (
        side_input_data.access_pattern == common_urns.side_inputs.MULTIMAP.urn)
    self._data = beam.pvalue.SideInputData(
        common_urns.side_inputs.MULTIMAP.urn,
        side_input_data.window_mapping_fn,
        side_input_data.view_fn)


class DataflowPipelineResult(PipelineResult):
  """Represents the state of a pipeline run on the Dataflow service."""

  def __init__(self, job, runner):
    """Initialize a new DataflowPipelineResult instance.

    Args:
      job: Job message from the Dataflow API. Could be :data:`None` if a job
        request was not sent to Dataflow service (e.g. template jobs).
      runner: DataflowRunner instance.
    """
    self._job = job
    self._runner = runner
    self.metric_results = None

  def _update_job(self):
    # We need the job id to be able to update job information. There is no need
    # to update the job if we are in a known terminal state.
    if self.has_job and not self.is_in_terminal_state():
      self._job = self._runner.dataflow_client.get_job(self.job_id())

  def job_id(self):
    return self._job.id

  def metrics(self):
    return self.metric_results

  @property
  def has_job(self):
    return self._job is not None

  def _get_job_state(self):
    values_enum = dataflow_api.Job.CurrentStateValueValuesEnum

    # Ordered by the enum values. Values that may be introduced in
    # future versions of Dataflow API are considered UNRECOGNIZED by the SDK.
    api_jobstate_map = defaultdict(lambda: PipelineState.UNRECOGNIZED, {
        values_enum.JOB_STATE_UNKNOWN: PipelineState.UNKNOWN,
        values_enum.JOB_STATE_STOPPED: PipelineState.STOPPED,
        values_enum.JOB_STATE_RUNNING: PipelineState.RUNNING,
        values_enum.JOB_STATE_DONE: PipelineState.DONE,
        values_enum.JOB_STATE_FAILED: PipelineState.FAILED,
        values_enum.JOB_STATE_CANCELLED: PipelineState.CANCELLED,
        values_enum.JOB_STATE_UPDATED: PipelineState.UPDATED,
        values_enum.JOB_STATE_DRAINING: PipelineState.DRAINING,
        values_enum.JOB_STATE_DRAINED: PipelineState.DRAINED,
        values_enum.JOB_STATE_PENDING: PipelineState.PENDING,
        values_enum.JOB_STATE_CANCELLING: PipelineState.CANCELLING,
    })

    return (api_jobstate_map[self._job.currentState] if self._job.currentState
            else PipelineState.UNKNOWN)

  @property
  def state(self):
    """Return the current state of the remote job.

    Returns:
      A PipelineState object.
    """
    if not self.has_job:
      return PipelineState.UNKNOWN

    self._update_job()

    return self._get_job_state()

  def is_in_terminal_state(self):
    if not self.has_job:
      return True

    return PipelineState.is_terminal(self._get_job_state())

  def wait_until_finish(self, duration=None):
    if not self.is_in_terminal_state():
      if not self.has_job:
        raise IOError('Failed to get the Dataflow job id.')

      thread = threading.Thread(
          target=DataflowRunner.poll_for_job_completion,
          args=(self._runner, self, duration))

      # Mark the thread as a daemon thread so a keyboard interrupt on the main
      # thread will terminate everything. This is also the reason we will not
      # use thread.join() to wait for the polling thread.
      thread.daemon = True
      thread.start()
      while thread.isAlive():
        time.sleep(5.0)

      # TODO: Merge the termination code in poll_for_job_completion and
      # is_in_terminal_state.
      terminated = self.is_in_terminal_state()
      assert duration or terminated, (
          'Job did not reach to a terminal state after waiting indefinitely.')

      if terminated and self.state != PipelineState.DONE:
        # TODO(BEAM-1290): Consider converting this to an error log based on
        # theresolution of the issue.
        raise DataflowRuntimeException(
            'Dataflow pipeline failed. State: %s, Error:\n%s' %
            (self.state, getattr(self._runner, 'last_error_msg', None)), self)
    return self.state

  def cancel(self):
    if not self.has_job:
      raise IOError('Failed to get the Dataflow job id.')

    self._update_job()

    if self.is_in_terminal_state():
      logging.warning(
          'Cancel failed because job %s is already terminated in state %s.',
          self.job_id(), self.state)
    else:
      if not self._runner.dataflow_client.modify_job_state(
          self.job_id(), 'JOB_STATE_CANCELLED'):
        cancel_failed_message = (
            'Failed to cancel job %s, please go to the Developers Console to '
            'cancel it manually.') % self.job_id()
        logging.error(cancel_failed_message)
        raise DataflowRuntimeException(cancel_failed_message, self)

    return self.state

  def __str__(self):
    return '<%s %s %s>' % (
        self.__class__.__name__,
        self.job_id(),
        self.state)

  def __repr__(self):
    return '<%s %s at %s>' % (self.__class__.__name__, self._job, hex(id(self)))


class DataflowRuntimeException(Exception):
  """Indicates an error has occurred in running this pipeline."""

  def __init__(self, msg, result):
    super(DataflowRuntimeException, self).__init__(msg)
    self.result = result
