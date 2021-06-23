# coding=utf-8
# Copyright 2021 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Beam pipeline for converting basel files to final output.

We get horrible fortran formatted text files from Basel. This pipeline
converts those into proto files, does all kinds of reprocessing and error
checking to produce the final outputs.
"""

import copy
import functools
import logging as stdlogging

from absl import app
from absl import flags
from absl import logging
import apache_beam as beam
from tensorflow.io import gfile

from google.protobuf import json_format
from smu import dataset_pb2
from smu.parser import smu_parser_lib
from smu.parser import smu_utils_lib
from smu.parser import smu_writer_lib


flags.DEFINE_string(
    'input_stage1_dat_glob', None,
    'Glob of stage1 dat files to read')
flags.DEFINE_string(
    'input_stage2_dat_glob', None,
    'Glob of stage2 dat files to read')
flags.DEFINE_string(
    'input_bond_topology_csv', None,
    'CSV file of bond topologies (see merge_bond_topologies)')
flags.DEFINE_string(
    'input_equivalent_glob', None,
    'Glob of files containing list of equivalent structure (usually '
    'list.equivalent_isomers.dat and list.equivalent_confomers.dat)')
flags.DEFINE_string(
    'output_stem', None,
    'Filestem for output files')
flags.DEFINE_integer('output_shards', 10,
                     'Number of output shards for our primary outputs')

FLAGS = flags.FLAGS

_METRICS_NAMESPACE = 'SMU'


def parse_equivalent_file(filename):
  """Parses the .dat of equivalent structure.

  The file is just pairs of entries where the first was kept over the second.
  Yields one entry per line keyed by the discarded conformer id.
  See merge_duplicate_information for how information is transferred to the kept
  conformer.

  Args:
    filename: string

  Yields:
    dataset_pb2.Conformer
  """
  with gfile.GFile(filename) as f:
    for line in f:
      kept_str, discard_str = line.split()
      _, _, kept_btid, kept_cid = smu_parser_lib.parse_long_identifier(kept_str)
      _, _, discard_btid, discard_cid = smu_parser_lib.parse_long_identifier(
          discard_str)
      # Convert to our conformer ids which include the btid
      kept_cid = kept_btid * 1000 + kept_cid
      discard_cid = discard_btid * 1000 + discard_cid

      yield dataset_pb2.Conformer(
          conformer_id=discard_cid, duplicated_by=kept_cid)


def parse_dat_file(filename, stage):
  """Beam pipeline component for reading dat files.

  Args:
    filename: filename to read
    stage: string 'stage1' or 'stage2'

  Yields:
    Pair of string (original dat), conformer
    conformer can be an Exception or a dataset_pb2.Conformer
  """
  smu_parser = smu_parser_lib.SmuParser(filename)
  if stage == 'stage1':
    process_fn = smu_parser.process_stage1
  else:
    process_fn = smu_parser.process_stage2
  for conformer, orig_dat_list in process_fn():
    orig_dat = '\n'.join(orig_dat_list) + '\n'

    beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                 stage + '_dat_entry_read').inc()

    yield orig_dat, conformer


def partition_parse_success(input_tuple, num_partitions, stage):
  """Function to beam.Partition parsed inputs based on parse success.

  Args:
    input_tuple: pair of orig_contents, conformer (see parse_dat_file)
    num_partitions: (should always be 3)
    stage: string 'stage1' or 'stage2'

  Returns:
    int (0 for success, 1, for known error, 2 for unknown error)
  """
  assert num_partitions == 3
  _, conformer = input_tuple
  if not isinstance(conformer, Exception):
    beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                 stage + '_parse_success').inc()
    return 0  # Parse success
  else:
    if isinstance(conformer, smu_parser_lib.SmuKnownError):
      beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                   stage + '_parse_known_error').inc()
      return 1  # Parse known error
    else:
      beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                   stage + '_parse_unknown_error').inc()
      return 2  # Parse unknown error


def regenerate_dat(input_tuple, stage):
  """Regenerates the original dat from conformer and compares it to original.

  Args:
    input_tuple: tuple of string (original contents), dataset_pb2.Conformer
    stage: string 'stage1' or 'stage2'

  Returns:
    original_dat, conformer, regenerated dat, int (0=mismatch, 1=match)
  """
  original_dat, conformer = input_tuple
  smu_writer = smu_writer_lib.SmuWriter(annotate=False)
  if stage == 'stage1':
    regen_dat = smu_writer.process_stage1_proto(conformer)
  else:
    regen_dat = smu_writer.process_stage2_proto(conformer)
  try:
    smu_writer_lib.check_dat_formats_match(original_dat.splitlines(),
                                           regen_dat.splitlines())
    beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                 stage + '_dat_format_matched').inc()
    return original_dat, conformer, regen_dat, 1
  except smu_writer_lib.DatFormatMismatchError:
    beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                 stage + '_dat_format_mismatched').inc()
    return original_dat, conformer, regen_dat, 0


def conformer_to_stat_values(conformer):
  """Beam transform to produce stats values for later aggregation.

  Each output will be a tuple of primary_key, secondary_key and these will be
  aggregated as counts.

  Args:
    conformer: dataset_pb2.Conformer

  Yields:
    primary_key, secondary_key
  """
  # Yield the values for all the error codes
  # We don't want to use ListFields here because we want 0 error values to
  # come out here (fields with default values are skipped in ListFields).
  for field_descriptor in conformer.properties.errors.DESCRIPTOR.fields:
    value = getattr(conformer.properties.errors, field_descriptor.name)
    # Force everything to int to simplify later processing
    if field_descriptor.name == 'error_during_merging':
      yield field_descriptor.name, len(value)
    else:
      yield field_descriptor.name, int(value)

  yield 'fate', dataset_pb2.Conformer.FateCategory.Name(conformer.fate)

  yield 'num_initial_geometries', len(conformer.initial_geometries)
  yield 'num_duplicates', len(conformer.duplicate_of)


def bond_topology_summaries_from_csv(filename):
  """Beam DoFn for generating bare BondTopologySummary.

  Args:
    filename: csv file of bond topologies to read

  Yields:
    dataset_pb2.Entry
  """
  for bt in smu_utils_lib.generate_bond_topologies_from_csv(filename):
    summary = dataset_pb2.BondTopologySummary()
    summary.bond_topology.CopyFrom(bt)
    # Note that we leave all the counts as 0.
    yield bt.bond_topology_id, summary


class MergeConformersFn(beam.DoFn):
  """Merges conformers with the same id.

  Because of the stage1, stage2, and duplicate information, we can end up with
  multiple conformers with the same id. This merges them.
  """
  OUTPUT_TAG_MERGE_CONFLICT = 'conflict'

  def process(self, args):
    """"Merges conformers.

    Args:
      args: tuple of conformer_id(should match the id in all conformers) and
            conformers(iterable of dataset_pb2.Conformer)

    Yields:
      dataset_pb2.Conformer and tagged output (OUTPUT_TAG_MERGE_CONFLICT) with
      conflict output from smu_utils_lib.merge_conformer
    """
    conformer_id, conformers = args

    for c in conformers:
      if c.conformer_id != conformer_id:
        raise ValueError(
            f'In merged CID {conformer_id}, found CID {c.conformer_id} instead')

    # For signalling the first merging.
    sentinel = object()

    conflicts = []

    def _merge_two_conformers(conf0, conf1):
      if conf0 is sentinel:
        return conf1

      merged_conf, merge_conflict = smu_utils_lib.merge_conformer(conf0, conf1)
      if merge_conflict:
        beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                     'conformer_merge_error').inc()
        conflicts.append(merge_conflict)
      return merged_conf

    beam.metrics.Metrics.counter(_METRICS_NAMESPACE, 'merged_conformers').inc()

    # Note that we convert the iterable to a list and do a deepcopy. We can't
    # modify the input and smu_utils_lib.merge_conformer wants to reserve the
    # right to modify either input so it's simplest to just copy it once right
    # off the bat.
    yield functools.reduce(_merge_two_conformers,
                           copy.deepcopy(list(conformers)),
                           sentinel)

    for c in conflicts:
      yield beam.pvalue.TaggedOutput(
          MergeConformersFn.OUTPUT_TAG_MERGE_CONFLICT, c)


class UpdateConformerFn(beam.DoFn):
  """DoFn that performs several updates to fields in Conformer.

  * Updates the smiles string (with a tagged output to record the mismatches.
  * Adds Fate field
  * TODO(ianwatson, pfr): add in the geometry sensing part here

  main output is dataset_pb2.Conformer
  smiles output is a tuple of
    conformer_id,
    SmilesCompareResult,
    original smiles,
    smiles_with_h,
    smiles_without_h
  """
  OUTPUT_TAG_SMILES_MISMATCH = 'tag_smiles'

  def _compare_smiles(self, conformer):
    if len(conformer.bond_topologies) != 1:
      raise ValueError(
          'compare_smiles expects 1 bond topology; for CID {} got {}'.format(
              conformer.conformer_id, len(conformer.bond_topologies)))

    result, smiles_with_h, smiles_without_h = (
        smu_utils_lib.bond_topology_smiles_comparison(
            conformer.bond_topologies[0]))
    if result != smu_utils_lib.SmilesCompareResult.MATCH:
      yield beam.pvalue.TaggedOutput(
          UpdateConformerFn.OUTPUT_TAG_SMILES_MISMATCH,
          (conformer.conformer_id,
           result,
           conformer.bond_topologies[0].smiles,
           smiles_with_h,
           smiles_without_h))
      conformer.bond_topologies[0].smiles = smiles_without_h

  def process(self, conformer):
    conformer = copy.deepcopy(conformer)

    conformer.fate = smu_utils_lib.determine_fate(conformer)

    yield from self._compare_smiles(conformer)

    yield conformer


def generate_keyed_conformers_for_duplicates(conformer):
  """Generates keyed conformers for duplicate merging.

  Every conformer yields itself keyed by its conformer_id
  Additonally, if duplicated_by is set, the conformer is yielded keyed by
  duplicated_by.

  Args:
    conformer: dataset_pb2.Conformer

  Yields:
    conformer_id, dataset_pb2.Conformer
  """
  yield conformer.conformer_id, conformer
  if conformer.duplicated_by > 0:
    yield conformer.duplicated_by, conformer


def merge_duplicate_information(conformer_id, conformers):
  """Merges duplicate information into one conformer.

  One entry in conformers should have the given conformer_id
  (call this the "main" conformer)
  Every other entry should have a duplicated_by set to conformer_id
  (call this an "other" conformer)

  The initial_geometry from other will copied to main.
  If the bond topology id is the same, this is trivial
  TODO(pfr, ianwatson): implement this copying with unequal bond topologies.

  Args:
    conformer_id: integer
    conformers: iterable of dataset_pb2.Conformer

  Returns:
    dataset_pb2.Conformer
  """
  matching_conformers = [c for c in conformers
                         if c.conformer_id == conformer_id]
  if len(matching_conformers) != 1:
    raise ValueError('Expected 1 conformers with id {}, got {}'.format(
        conformer_id, len(matching_conformers)))
  main_conformer = copy.deepcopy(matching_conformers[0])

  for conf in conformers:
    if conf.conformer_id == conformer_id:
      continue
    if conf.duplicated_by != conformer_id:
      raise ValueError(
          'Conformer {} should have duplicated_by {} but has {}'.format(
              conf.conformer_id, conformer_id, conf.duplicated_by))
    main_conformer.duplicate_of.append(conf.conformer_id)
    if conformer_id // 1000 == conf.conformer_id // 1000:
      # easy case! Bond topologies are the same, just copy over
      main_conformer.initial_geometries.append(conf.initial_geometries[0])
      beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                   'dup_same_topology').inc()
    else:
      # hard case. We have to figure out out to permute the atoms in the initial
      # geometry
      # TODO(pfr, ianwatson)
      beam.metrics.Metrics.counter(_METRICS_NAMESPACE,
                                   'dup_diff_topology_unmatched').inc()
      pass

  return main_conformer


def to_keyed_bond_topology_summary(conformer):
  """Outputs BondTopologySummary for conformer.

  Args:
    conformer: dataset_pb2.Conformer

  Yields:
    bond topology id, BondTopologySummary
  """
  for summary in smu_utils_lib.conformer_to_bond_topology_summaries(conformer):
    yield summary.bond_topology.bond_topology_id, summary


def merge_bond_topology_summaries(summaries, field_names):
  """Merges BondToplogySummary protos.

  See CombineAndWriteBondTopologySummary for context.

  Args:
    summaries: iterable of BondTopologySummary
    field_names: list of field names to be aggregated

  Returns:
    BondTopologySummary
  """
  # For signalling the first merging.
  sentinel = object()

  def _merge_two_summaries(summary0, summary1):
    if summary0 is sentinel:
      # We'll just make one copy and use the sentinel to tell us when to do
      # that
      return copy.deepcopy(summary1)

    assert (summary0.bond_topology.bond_topology_id ==
            summary1.bond_topology.bond_topology_id)

    for name in field_names:
      setattr(summary0, name, getattr(summary0, name) + getattr(summary1, name))

    return summary0

  beam.metrics.Metrics.counter(_METRICS_NAMESPACE, 'merged_summaries').inc()

  return functools.reduce(_merge_two_summaries, summaries, sentinel)


def csv_format_bond_topology_summary(summary, field_names):
  """Formats BondToplogySummary protos as csv line.

  See CombineAndWriteBondTopologySummary for context.

  Args:
    summary: BondTopologySummary
    field_names: list of field names in the order for the csv

  Returns:
    BondTopologySummary
  """
  return ','.join([str(summary.bond_topology.bond_topology_id)] +
                  [str(getattr(summary, name))
                   for name in field_names])


class CombineAndWriteBondTopologySummary(beam.PTransform):
  """A composite transform for handling BondTopologySummary.

  The only reason we make this a composite transform is that multiple places
  need the list of count fields in BondTopologySummary, so we make it
  one time with a consistent ordering and use it in multiple places.
  """

  def expand(self, pcoll):
    field_names = []
    for field_descriptor in dataset_pb2.BondTopologySummary.DESCRIPTOR.fields:
      if field_descriptor.name.startswith('count_'):
        field_names.append(field_descriptor.name)

    return (pcoll
            | 'CombineByBTID' >> beam.CombinePerKey(
                merge_bond_topology_summaries, field_names=field_names)
            | 'DropBTID' >> beam.Values()
            | 'Reshuffle' >> beam.Reshuffle()
            | 'CSVFormat' >> beam.Map(
                csv_format_bond_topology_summary, field_names=field_names)
            | 'WriteCSV' >> beam.io.WriteToText(
                FLAGS.output_stem + '_bt_summary',
                header='bt_id,' + ','.join(field_names),
                num_shards=1,
                file_name_suffix='.csv'))


def make_complete_conformer(conformer):
  """Turns a Conformer into the complete form from the internal only.

  Args:
    conformer: dataset_pb2.Conformer

  Returns:
    dataset_pb2.Conformer
  """
  out = copy.deepcopy(conformer)
  smu_utils_lib.filter_conformer_by_availability(
      out, [dataset_pb2.STANDARD, dataset_pb2.COMPLETE])

  beam.metrics.Metrics.counter(_METRICS_NAMESPACE, 'complete_conformers').inc()

  return out


def make_standard_conformer(conformer):
  """Turns a Conformer into the standard form from the internal only.

  This must go through a FlatMap because some conformers are filtered.

  Args:
    conformer: dataset_pb2.Conformer

  Yields:
    at most one dataset_pb2.Conformer
  """
  out = copy.deepcopy(conformer)
  if not smu_utils_lib.conformer_to_standard(out):
    return

  beam.metrics.Metrics.counter(_METRICS_NAMESPACE, 'standard_conformers').inc()

  yield out


def key_to_string(key, value):
  return str(key), value


def csv_format(vals):
  return ','.join(str(v) for v in vals)


def conformer_to_json(conformer):
  return json_format.MessageToJson(
      conformer,
      preserving_proto_field_name=True,
      including_default_value_fields=True)


def dat_input_and_parsing_pipeline(root, stage):
  """Create multiple stages for parsing and validation .dat files.

  We read two types of .dat files, stage1 and stage2. The pipeline is similar
  between the two, so we use this function to cover those similar parts.

  Args:
    root: root of the pipeline
    stage: string that is either "stage1" or "stage2"

  Returns:
    PCollection of dataset_pb2.Conformer that are valid and matched
  """
  assert stage in ['stage1', 'stage2']

  label = stage.title()

  # Parse the files and split in collections based on whether the parsing worked
  if stage == 'stage1':
    input_files = gfile.glob(FLAGS.input_stage1_dat_glob)
  else:
    input_files = gfile.glob(FLAGS.input_stage2_dat_glob)
  parsed_inputs = (
      root
      | 'CreateInputs' + label >> beam.Create(input_files)
      | 'ReshuffleInput' + label >> beam.Reshuffle()
      | 'ParseDat' + label >> beam.FlatMap(parse_dat_file, stage)
      )
  parsed_success, parsed_known_error, parsed_unknown_error = (
      parsed_inputs
      | 'PartitionParseError' + label >> beam.Partition(partition_parse_success,
                                                        3, stage))

  # For the parse errors, write out the original contents to files to be
  # examined later.
  _ = (
      parsed_known_error
      | 'ParsedKnownErrorReshuffle' + label >> beam.Reshuffle()
      | 'ExtractOriginalKnown' + label >>
      beam.MapTuple(lambda orig_dat, _: orig_dat)
      | 'WriteOriginalKnown' + label >> beam.io.WriteToText(
          FLAGS.output_stem + '_' + stage +'_original_known_error',
          num_shards=1,
          file_name_suffix='.dat'))
  _ = (
      parsed_unknown_error
      | 'ParsedUnknownErrorReshuffle' + label >> beam.Reshuffle()
      | 'ExtractOriginalUnknown' + label >>
      beam.MapTuple(lambda orig_dat, _: orig_dat)
      | 'WriteOriginalUnknown' + label >> beam.io.WriteToText(
          FLAGS.output_stem + '_' + stage + '_original_unknown_error',
          num_shards=1,
          file_name_suffix='.dat'))

  mismatched, matched = (
      parsed_success
      | 'RegenerateDat' + label >> beam.Map(regenerate_dat, stage)
      | 'PartitionByMatch' + label >> beam.Partition(lambda x, _: x[3], 2))

  # Write out the mismatched conformers, original and regenerated
  # Reshuffle before the forced write of a single shard
  reshuffled_mismatched = (
      mismatched
      | 'MismatchedReshuffle' + label >> beam.Reshuffle())
  _ = (
      reshuffled_mismatched
      | 'ExtractMismatchedOriginal' + label >> beam.Map(lambda x: x[0])
      | 'WriteMismatchedOriginal' + label >> beam.io.WriteToText(
          FLAGS.output_stem + '_' + stage + '_mismatched_original',
          num_shards=1,
          file_name_suffix='.dat'))
  _ = (
      reshuffled_mismatched
      | 'ExtractMismatchedRegen' + label >> beam.Map(lambda x: x[2])
      | 'WriteMismatchedRegen' + label >> beam.io.WriteToText(
          FLAGS.output_stem + '_' + stage + '_mismatched_regen',
          num_shards=1,
          file_name_suffix='.dat'))

  matched_conformers = (
      matched
      | 'ExtractMatchedConformer' + label >> beam.Map(lambda x: x[1]))

  return matched_conformers


def pipeline(root):
  """Beam pipeline.

  Args:
    root: the root of the pipeline.
  """
  stage1_matched_conformers = dat_input_and_parsing_pipeline(root, 'stage1')
  stage2_matched_conformers = dat_input_and_parsing_pipeline(root, 'stage2')

  # Create a collection of conformers with duplicate information
  equivalent_files = gfile.glob(FLAGS.input_equivalent_glob)
  equivalent_conformers = (
      root
      | 'CreateEquivInputs' >> beam.Create(equivalent_files)
      | 'ParseEquiv' >> beam.FlatMap(parse_equivalent_file)
      )

  # Merge by bond_topology_id
  merged_results = (
      (stage1_matched_conformers, stage2_matched_conformers,
       equivalent_conformers)
      | 'FlattenAllConformers' >> beam.Flatten()
      | 'GroupByCID' >> beam.GroupBy(lambda c: c.conformer_id)
      | 'MergeConformers' >> beam.ParDo(MergeConformersFn()).with_outputs(
          MergeConformersFn.OUTPUT_TAG_MERGE_CONFLICT, main='conformers'))
  merged_conformers = merged_results['conformers']

  # Write out the merge conflicts
  _ = (
      merged_results[MergeConformersFn.OUTPUT_TAG_MERGE_CONFLICT]
      | 'ConflictsCSVFormat' >> beam.Map(csv_format)
      | 'ConflictsReshuffle' >> beam.Reshuffle()
      | 'WriteConflictsCSV' >> beam.io.WriteToText(
          FLAGS.output_stem + '_conflicts',
          header=csv_format(smu_utils_lib.MERGE_CONFLICT_FIELDS),
          num_shards=1,
          file_name_suffix='.csv'))

  # Various per conformer processing
  update_results = (
      merged_conformers
      | 'UpdateConformers' >> beam.ParDo(UpdateConformerFn()).with_outputs(
          UpdateConformerFn.OUTPUT_TAG_SMILES_MISMATCH, main='conformers'))
  updated_conformers = update_results['conformers']

  # Output SMILES mismatches
  _ = (
      update_results[UpdateConformerFn.OUTPUT_TAG_SMILES_MISMATCH]
      | 'ReshuffleSmilesOutput' >> beam.Reshuffle()
      |
      'SmilesCSVFormat' >> beam.Map(csv_format)
      | 'WriteSmilesCSV' >> beam.io.WriteToText(
          FLAGS.output_stem + '_smiles_compare',
          header='conformer_id,compare,smiles_given,smiles_with_h,smiles_without_h',
          num_shards=1,
          file_name_suffix='.csv'))

  # Process duplicate information
  final_conformers = (
      updated_conformers
      | 'KeyedForDuplicates' >>
      beam.FlatMap(generate_keyed_conformers_for_duplicates)
      | 'DupGroupByKey' >> beam.GroupByKey()
      | 'MergeDupInfo' >> beam.MapTuple(merge_duplicate_information))

  # Pull the stats of various sorts write to a file
  _ = (
      final_conformers
      | 'ExtractStats' >> beam.FlatMap(conformer_to_stat_values)
      | 'CountStats' >> beam.combiners.Count.PerElement()
      | 'StatsCSVFormat' >> beam.MapTuple(lambda x, c: f'{x[0]},{x[1]},{c}')
      | 'WriteStatsCSV' >> beam.io.WriteToText(
          FLAGS.output_stem + '_stats',
          header='primary_key,secondary_key,count',
          num_shards=1,
          file_name_suffix='.csv'))

  # Generate the summary by bond topology.
  bare_bt_summaries = (
      root
      | 'BondTopologyInput' >> beam.Create([FLAGS.input_bond_topology_csv])
      | 'GenerateBareBTSummaries' >>
      beam.FlatMap(bond_topology_summaries_from_csv))
  real_bt_summaries = (
      final_conformers
      | 'GenerateBTSummaries' >> beam.FlatMap(to_keyed_bond_topology_summary))
  _ = ((bare_bt_summaries, real_bt_summaries)
       | 'FlattenAllBTSummaries' >> beam.Flatten()
       | 'FinishBTSummary' >> CombineAndWriteBondTopologySummary())

  # Make the filtered versions of the dataset
  complete_conformers = (
      final_conformers
      | 'MakeComplete' >> beam.Map(make_complete_conformer))

  standard_conformers = (
      final_conformers
      | 'MakeStandard' >> beam.FlatMap(make_standard_conformer))

  # Write the complete and standard conformers as binary protobuf in TFRecord.
  for id_str, collection in [
      ['complete', complete_conformers],
      ['standard', standard_conformers]]:
    _ = (
        collection
        | ('TFRecordReshuffle_' + id_str) >> beam.Reshuffle()
        | ('WriteTFRecord_' + id_str) >> beam.io.tfrecordio.WriteToTFRecord(
            f'{FLAGS.output_stem}_{id_str}_tfrecord',
            coder=beam.coders.ProtoCoder(dataset_pb2.Conformer),
            num_shards=FLAGS.output_shards))


  # Write the complete and standard conformers as JSON.
  # Bit of a hack here: the slowest part of the whole pipeline is writing out
  # the JSON for the complete conformers. So we just hard code a tripling of the
  # shards to get more parallelism.
  for id_str, collection, num_shards in [
      ['complete', complete_conformers, FLAGS.output_shards * 3],
      ['standard', standard_conformers, FLAGS.output_shards]]:
    _ = (
        collection
        | ('JSONReshuffle_' + id_str) >> beam.Reshuffle()
        | ('ToJSON_' + id_str) >> beam.Map(conformer_to_json)
        | ('WriteJSON_' + id_str) >> beam.io.WriteToText(
            f'{FLAGS.output_stem}_{id_str}_json',
            num_shards=num_shards,
            file_name_suffix='.json.gz'))


def main(argv):
  if len(argv) > 1:
    raise app.UsageError('Too many command-line arguments.')
  stdlogging.getLogger().setLevel(stdlogging.INFO)
  logging.info('Pipeline Starts.')
  # If you have custom beam options, add them here.
  beam_options = None
  with beam.Pipeline(beam_options) as root:
    pipeline(root)


if __name__ == '__main__':
  app.run(main)
