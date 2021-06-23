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

"""Tests for smu_utils_lib."""

import copy
import os
import tempfile

from absl.testing import absltest
from absl.testing import parameterized
import numpy as np
import pandas as pd
from rdkit import Chem

from google.protobuf import text_format
from smu import dataset_pb2
from smu.parser import smu_parser_lib
from smu.parser import smu_utils_lib

MAIN_DAT_FILE = 'x07_sample.dat'
STAGE1_DAT_FILE = 'x07_stage1.dat'
TESTDATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'testdata')


def str_to_bond_topology(s):
  bt = dataset_pb2.BondTopology()
  text_format.Parse(s, bt)
  return bt


def get_stage1_conformer():
  parser = smu_parser_lib.SmuParser(
      os.path.join(TESTDATA_PATH, STAGE1_DAT_FILE))
  conformer, _ = next(parser.process_stage1())
  return conformer


def get_stage2_conformer():
  parser = smu_parser_lib.SmuParser(os.path.join(TESTDATA_PATH, MAIN_DAT_FILE))
  conformer, _ = next(parser.process_stage2())
  return conformer


class SpecialIDTest(absltest.TestCase):

  def test_from_dat_id(self):
    self.assertIsNone(
        smu_utils_lib.special_case_bt_id_from_dat_id(123456, 'CC'))
    self.assertEqual(smu_utils_lib.special_case_bt_id_from_dat_id(999998, 'O'),
                     899650)
    self.assertEqual(smu_utils_lib.special_case_bt_id_from_dat_id(0, 'O'),
                     899650)
    with self.assertRaises(ValueError):
      smu_utils_lib.special_case_bt_id_from_dat_id(0, 'NotASpecialCaseSmiles')

  def test_from_bt_id(self):
    self.assertIsNone(smu_utils_lib.special_case_dat_id_from_bt_id(123456))
    self.assertEqual(
        smu_utils_lib.special_case_dat_id_from_bt_id(899651), 999997)


class GetCompositionTest(absltest.TestCase):

  def test_simple(self):
    bt = dataset_pb2.BondTopology()
    bt.atoms.extend([dataset_pb2.BondTopology.ATOM_C,
                     dataset_pb2.BondTopology.ATOM_C,
                     dataset_pb2.BondTopology.ATOM_N,
                     dataset_pb2.BondTopology.ATOM_H,
                     dataset_pb2.BondTopology.ATOM_H,
                     dataset_pb2.BondTopology.ATOM_H])
    self.assertEqual('x03_c2nh3', smu_utils_lib.get_composition(bt))


class GetCanonicalStoichiometryWithHydrogensTest(absltest.TestCase):

  def test_cyclobutane(self):
    bt = smu_utils_lib.create_bond_topology('CCCC', '110011', '2222')
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(bt), '(ch2)4')

  def test_ethylene(self):
    bt = smu_utils_lib.create_bond_topology('CC', '2', '22')
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(bt), '(ch2)2')

  def test_acrylic_acid(self):
    bt = smu_utils_lib.create_bond_topology('CCCOO', '2000100210', '21001')
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(bt),
        '(c)(ch)(ch2)(o)(oh)')

  def test_fluorine(self):
    bt = smu_utils_lib.create_bond_topology('OFF', '110', '000')
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(bt), '(o)(f)2')

  def test_fully_saturated(self):
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(
            smu_utils_lib.create_bond_topology('C', '', '4')), '(ch4)')
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(
            smu_utils_lib.create_bond_topology('N', '', '3')), '(nh3)')
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(
            smu_utils_lib.create_bond_topology('O', '', '2')), '(oh2)')
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(
            smu_utils_lib.create_bond_topology('F', '', '1')), '(fh)')

  def test_nplus_oneg(self):
    bt = smu_utils_lib.create_bond_topology('NO', '1', '30')
    self.assertEqual(
        smu_utils_lib.get_canonical_stoichiometry_with_hydrogens(bt),
        '(nh3)(o)')


class ParseBondTopologyTest(absltest.TestCase):

  def test_4_heavy(self):
    num_atoms, atoms_str, matrix, hydrogens = smu_utils_lib.parse_bond_topology_line(
        ' 4  N+O O O-  010110  3000')
    self.assertEqual(num_atoms, 4)
    self.assertEqual(atoms_str, 'N+O O O-')
    self.assertEqual(matrix, '010110')
    self.assertEqual(hydrogens, '3000')

  def test_7_heavy(self):
    num_atoms, atoms_str, matrix, hydrogens = smu_utils_lib.parse_bond_topology_line(
        ' 7  N+O O O O-F F   001011101001000000000  1000000')
    self.assertEqual(num_atoms, 7)
    self.assertEqual(atoms_str, 'N+O O O O-F F ')  # Note the trailing space
    self.assertEqual(matrix, '001011101001000000000')
    self.assertEqual(hydrogens, '1000000')


class CreateBondTopologyTest(absltest.TestCase):

  def test_no_charged(self):
    got = smu_utils_lib.create_bond_topology('CNFF', '111000', '1200')
    expected_str = '''
atoms: ATOM_C
atoms: ATOM_N
atoms: ATOM_F
atoms: ATOM_F
atoms: ATOM_H
atoms: ATOM_H
atoms: ATOM_H
bonds {
  atom_b: 1
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 2
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 3
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 4
  bond_type: BOND_SINGLE
}
bonds {
  atom_a: 1
  atom_b: 5
  bond_type: BOND_SINGLE
}
bonds {
  atom_a: 1
  atom_b: 6
  bond_type: BOND_SINGLE
}
'''
    expected = str_to_bond_topology(expected_str)
    self.assertEqual(str(expected), str(got))

  def test_charged(self):
    # This is actually C N N+O-
    got = smu_utils_lib.create_bond_topology('CNNO', '200101', '2020')
    expected_str = '''
atoms: ATOM_C
atoms: ATOM_N
atoms: ATOM_NPOS
atoms: ATOM_ONEG
atoms: ATOM_H
atoms: ATOM_H
atoms: ATOM_H
atoms: ATOM_H
bonds {
  atom_b: 1
  bond_type: BOND_DOUBLE
}
bonds {
  atom_a: 1
  atom_b: 2
  bond_type: BOND_SINGLE
}
bonds {
  atom_a: 2
  atom_b: 3
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 4
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 5
  bond_type: BOND_SINGLE
}
bonds {
  atom_a: 2
  atom_b: 6
  bond_type: BOND_SINGLE
}
bonds {
  atom_a: 2
  atom_b: 7
  bond_type: BOND_SINGLE
}
'''
    expected = str_to_bond_topology(expected_str)
    self.assertEqual(str(expected), str(got))

  def test_one_heavy(self):
    got = smu_utils_lib.create_bond_topology('C', '', '4')
    expected_str = '''
atoms: ATOM_C
atoms: ATOM_H
atoms: ATOM_H
atoms: ATOM_H
atoms: ATOM_H
bonds {
  atom_b: 1
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 2
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 3
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 4
  bond_type: BOND_SINGLE
}
'''
    expected = str_to_bond_topology(expected_str)
    self.assertEqual(str(expected), str(got))


class FromCSVTest(absltest.TestCase):

  def test_basic(self):
    infile = tempfile.NamedTemporaryFile(mode='w', delete=False)
    infile.write(
        'id,num_atoms,atoms_str,connectivity_matrix,hydrogens,smiles\n')
    infile.write('68,3,C N+O-,310,010,[NH+]#C[O-]\n')
    infile.write('134,4,N+O-F F ,111000,1000,[O-][NH+](F)F\n')
    infile.close()

    out = smu_utils_lib.generate_bond_topologies_from_csv(infile.name)

    bt = next(out)
    self.assertEqual(68, bt.bond_topology_id)
    self.assertLen(bt.atoms, 4)
    self.assertEqual(bt.smiles, '[NH+]#C[O-]')

    bt = next(out)
    self.assertEqual(134, bt.bond_topology_id)
    self.assertLen(bt.atoms, 5)
    self.assertEqual(bt.smiles, '[O-][NH+](F)F')


class ParseDuplicatesFileTest(absltest.TestCase):

  def test_basic(self):
    df = smu_utils_lib.parse_duplicates_file(
        os.path.join(TESTDATA_PATH, 'small.equivalent_isomers.dat'))
    pd.testing.assert_frame_equal(
        pd.DataFrame(
            columns=['name1', 'stoich1', 'btid1', 'shortconfid1', 'confid1',
                     'name2', 'stoich2', 'btid2', 'shortconfid2', 'confid2'],
            data=[
                ['x07_c2n2o2fh3.224227.004',
                 'c2n2o2fh3', 224227, 4, 224227004,
                 'x07_c2n2o2fh3.224176.005',
                 'c2n2o2fh3', 224176, 5, 224176005],
                ['x07_c2n2o2fh3.260543.005',
                 'c2n2o2fh3', 260543, 5, 260543005,
                 'x07_c2n2o2fh3.224050.001',
                 'c2n2o2fh3', 224050, 1, 224050001],
            ]),
        df,
        check_like=True)


class BondTopologyToMoleculeTest(absltest.TestCase):

  def test_o2(self):
    bond_topology = str_to_bond_topology('''
atoms: ATOM_O
atoms: ATOM_O
bonds {
  atom_b: 1
  bond_type: BOND_DOUBLE
}
''')
    got = smu_utils_lib.bond_topology_to_molecule(bond_topology)
    self.assertEqual('O=O', Chem.MolToSmiles(got))

  def test_methane(self):
    bond_topology = str_to_bond_topology('''
atoms: ATOM_C
atoms: ATOM_H
atoms: ATOM_H
atoms: ATOM_H
atoms: ATOM_H
bonds {
  atom_b: 1
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 2
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 3
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 4
  bond_type: BOND_SINGLE
}
''')
    got = smu_utils_lib.bond_topology_to_molecule(bond_topology)
    self.assertEqual('[H]C([H])([H])[H]', Chem.MolToSmiles(got))

  # This molecule is an N+ central atom, bonded to C (triply), O-, and F
  def test_charged_molecule(self):
    bond_topology = str_to_bond_topology('''
atoms: ATOM_C
atoms: ATOM_NPOS
atoms: ATOM_ONEG
atoms: ATOM_F
bonds {
  atom_b: 1
  bond_type: BOND_TRIPLE
}
bonds {
  atom_a: 1
  atom_b: 2
  bond_type: BOND_SINGLE
}
bonds {
  atom_a: 1
  atom_b: 3
  bond_type: BOND_SINGLE
}
''')
    got = smu_utils_lib.bond_topology_to_molecule(bond_topology)
    self.assertEqual('C#[N+]([O-])F', Chem.MolToSmiles(got))


class ConformerToMoleculeTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.conformer = get_stage2_conformer()

    # We'll make a new initial_geometry which is just the current one with all
    # coordinates multiplied by 1000
    self.conformer.initial_geometries.append(
        self.conformer.initial_geometries[0])
    new_geom = self.conformer.initial_geometries[1]
    for atom_pos in new_geom.atom_positions:
      atom_pos.x = atom_pos.x * 1000
      atom_pos.y = atom_pos.y * 1000
      atom_pos.z = atom_pos.z * 1000

    # For the extra bond_topology, we'll just copy the existing one and change
    # the id. Through the dumb luck of the molecule we picked there's not a
    # simple way to make this a new bond topology and still have it look valid
    # to RDKit
    self.conformer.bond_topologies.append(self.conformer.bond_topologies[0])
    self.conformer.bond_topologies[1].bond_topology_id = 99999

  def test_all_outputs(self):
    mols = list(smu_utils_lib.conformer_to_molecules(self.conformer))
    self.assertLen(mols, 6)  # 2 bond topologies * (1 opt geom + 2 init_geom)
    self.assertEqual([m.GetProp('_Name') for m in mols], [
        'SMU 618451001 bt=618451(0/2) geom=init(0/2)',
        'SMU 618451001 bt=618451(0/2) geom=init(1/2)',
        'SMU 618451001 bt=618451(0/2) geom=opt',
        'SMU 618451001 bt=99999(1/2) geom=init(0/2)',
        'SMU 618451001 bt=99999(1/2) geom=init(1/2)',
        'SMU 618451001 bt=99999(1/2) geom=opt'
    ])
    self.assertEqual(
        '[H]C(F)=C(OC([H])([H])[H])OC([H])([H])[H]',
        Chem.MolToSmiles(mols[0], kekuleSmiles=True, isomericSmiles=False))
    self.assertEqual(
        '[H]C(F)=C(OC([H])([H])[H])OC([H])([H])[H]',
        Chem.MolToSmiles(mols[4], kekuleSmiles=True, isomericSmiles=False))

  def test_initial_only(self):
    mols = list(
        smu_utils_lib.conformer_to_molecules(
            self.conformer,
            include_initial_geometries=True,
            include_optimized_geometry=False,
            include_all_bond_topologies=False))
    self.assertLen(mols, 2)
    self.assertEqual([m.GetProp('_Name') for m in mols], [
        'SMU 618451001 bt=618451(0/2) geom=init(0/2)',
        'SMU 618451001 bt=618451(0/2) geom=init(1/2)',
    ])
    # This is just one random atom I picked from the .dat file and converted to
    # angstroms instead of bohr.
    self.assertEqual('C', mols[0].GetAtomWithIdx(1).GetSymbol())
    np.testing.assert_allclose([0.6643, -3.470301, 3.4766],
                               list(mols[0].GetConformer().GetAtomPosition(1)),
                               atol=1e-6)

    self.assertEqual('C', mols[1].GetAtomWithIdx(1).GetSymbol())
    np.testing.assert_allclose([664.299998, -3470.300473, 3476.600215],
                               list(mols[1].GetConformer().GetAtomPosition(1)),
                               atol=1e-6)

  def test_optimized_only(self):
    mols = list(
        smu_utils_lib.conformer_to_molecules(
            self.conformer,
            include_initial_geometries=False,
            include_optimized_geometry=True,
            include_all_bond_topologies=False))
    self.assertLen(mols, 1)
    self.assertEqual(
        mols[0].GetProp('_Name'),
        'SMU 618451001 bt=618451(0/2) geom=opt',
    )
    self.assertEqual(
        '[H]C(F)=C(OC([H])([H])[H])OC([H])([H])[H]',
        Chem.MolToSmiles(mols[0], kekuleSmiles=True, isomericSmiles=False))
    # This is just two random atoms I picked from the .dat file and converted to
    # angstroms instead of bohr.
    self.assertEqual('C', mols[0].GetAtomWithIdx(1).GetSymbol())
    np.testing.assert_allclose([0.540254, -3.465543, 3.456982],
                               list(mols[0].GetConformer().GetAtomPosition(1)),
                               atol=1e-6)
    self.assertEqual('H', mols[0].GetAtomWithIdx(13).GetSymbol())
    np.testing.assert_allclose([2.135153, -1.817366, 0.226376],
                               list(mols[0].GetConformer().GetAtomPosition(13)),
                               atol=1e-6)


class SmilesCompareTest(absltest.TestCase):

  def test_string_format(self):
    # for some simplicity later on, we use shorter names
    self.assertEqual('MISSING', str(smu_utils_lib.SmilesCompareResult.MISSING))
    self.assertEqual('MISMATCH',
                     str(smu_utils_lib.SmilesCompareResult.MISMATCH))
    self.assertEqual('MATCH', str(smu_utils_lib.SmilesCompareResult.MATCH))

  def test_missing(self):
    bond_topology = str_to_bond_topology('''
atoms: ATOM_O
atoms: ATOM_O
bonds {
  atom_b: 1
  bond_type: BOND_DOUBLE
}
''')
    result, with_h, without_h = smu_utils_lib.bond_topology_smiles_comparison(
        bond_topology)
    self.assertEqual(smu_utils_lib.SmilesCompareResult.MISSING, result)
    self.assertEqual('O=O', with_h)
    self.assertEqual('O=O', without_h)

    # Also directly test compute_smiles_for_bond_topology
    self.assertEqual(
        'O=O',
        smu_utils_lib.compute_smiles_for_bond_topology(
            bond_topology, include_hs=True))

  def test_mismatch(self):
    bond_topology = str_to_bond_topology('''
atoms: ATOM_O
atoms: ATOM_O
bonds {
  atom_b: 1
  bond_type: BOND_DOUBLE
}
smiles: "BlahBlahBlah"
''')
    result, with_h, without_h = smu_utils_lib.bond_topology_smiles_comparison(
        bond_topology)
    self.assertEqual(smu_utils_lib.SmilesCompareResult.MISMATCH, result)
    self.assertEqual('O=O', with_h)
    self.assertEqual('O=O', without_h)

  def test_matched_and_h_stripping(self):
    bond_topology = str_to_bond_topology('''
atoms: ATOM_O
atoms: ATOM_H
atoms: ATOM_H
bonds {
  atom_b: 1
  bond_type: BOND_SINGLE
}
bonds {
  atom_b: 2
  bond_type: BOND_SINGLE
}
smiles: "O"
''')
    result, with_h, without_h = smu_utils_lib.bond_topology_smiles_comparison(
        bond_topology)
    self.assertEqual(smu_utils_lib.SmilesCompareResult.MATCH, result)
    self.assertEqual('[H]O[H]', with_h)
    self.assertEqual('O', without_h)

    # Also directly test compute_smiles_for_bond_topology
    self.assertEqual(
        '[H]O[H]',
        smu_utils_lib.compute_smiles_for_bond_topology(
            bond_topology, include_hs=True))
    self.assertEqual(
        'O',
        smu_utils_lib.compute_smiles_for_bond_topology(
            bond_topology, include_hs=False))

  def test_compute_smiles_from_molecule_no_hs(self):
    mol = Chem.MolFromSmiles('FOC', sanitize=False)
    self.assertEqual(
        smu_utils_lib.compute_smiles_for_molecule(mol, include_hs=False), 'COF')
    # This is expected. Even with include_hs=True, if there were no Hs in the
    # molecule, they will not be in the smiles.
    self.assertEqual(
        smu_utils_lib.compute_smiles_for_molecule(mol, include_hs=True), 'COF')

  def test_compute_smiles_from_molecule_with_hs(self):
    mol = Chem.MolFromSmiles('FOC', sanitize=False)
    Chem.SanitizeMol(mol, Chem.rdmolops.SanitizeFlags.SANITIZE_ADJUSTHS)
    mol = Chem.AddHs(mol)
    self.assertEqual(
        smu_utils_lib.compute_smiles_for_molecule(mol, include_hs=False), 'COF')
    self.assertEqual(
        smu_utils_lib.compute_smiles_for_molecule(mol, include_hs=True),
        '[H]C([H])([H])OF')

  def test_compute_smiles_from_molecule_special_case(self):
    mol = Chem.MolFromSmiles('C12=C3C4=C1C4=C23', sanitize=False)
    # Double check that this really is the special case -- we get back the
    # SMILES we put in even though it's not the one we want.
    self.assertEqual('C12=C3C4=C1C4=C23',
                     Chem.MolToSmiles(mol, kekuleSmiles=True))
    self.assertEqual(
        smu_utils_lib.compute_smiles_for_molecule(mol, include_hs=False),
        'C12=C3C1=C1C2=C31')

  def test_compute_smiles_from_molecule_labeled_with_h(self):
    mol = Chem.MolFromSmiles(
        '[O-][N+]([H])([H])N([H])OC([H])([H])F', sanitize=False)
    self.assertIsNotNone(mol)
    self.assertEqual(
        '[O-][N+:1]([H:2])([H:3])[N:4]([H:5])[O:6][C:7]([H:8])([H:9])[F:10]',
        smu_utils_lib.compute_smiles_for_molecule(
            mol, include_hs=True, labeled_atoms=True))

  def test_compute_smiles_from_molecule_labeled_no_h(self):
    mol = Chem.MolFromSmiles(
        '[O-][N+]([H])([H])N([H])OC([H])([H])F', sanitize=False)
    self.assertIsNotNone(mol)
    self.assertEqual(
        '[O-][NH2+:1][NH:2][O:3][CH2:4][F:5]',
        smu_utils_lib.compute_smiles_for_molecule(
            mol, include_hs=False, labeled_atoms=True))


class MergeConformersTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    # We are relying on the fact that the first conformer in both x07_sample.dat
    # and x07_stage1.dat are the same.
    self.stage1_conformer = get_stage1_conformer()
    self.stage2_conformer = get_stage2_conformer()

    self.duplicate_conformer = dataset_pb2.Conformer()
    self.duplicate_conformer.conformer_id = self.stage1_conformer.conformer_id
    # A real duplicate conformer wouldn't have both of these fields filled in,
    # but it's fine for the test to make sure everything is copied.
    self.duplicate_conformer.duplicated_by = 123
    self.duplicate_conformer.duplicate_of.extend([111, 222])

  def test_two_stage2(self):
    with self.assertRaises(ValueError):
      smu_utils_lib.merge_conformer(self.stage2_conformer,
                                    self.stage2_conformer)

  def test_two_stage1(self):
    with self.assertRaises(ValueError):
      smu_utils_lib.merge_conformer(self.stage1_conformer,
                                    self.stage1_conformer)

  def test_two_duplicates(self):
    duplicate_conformer2 = copy.deepcopy(self.duplicate_conformer)
    duplicate_conformer2.duplicate_of[:] = [333, 444]

    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.duplicate_conformer, duplicate_conformer2)
    self.assertIsNone(got_conflict)
    self.assertEqual(123, got_conf.duplicated_by)
    self.assertCountEqual([111, 222, 333, 444], got_conf.duplicate_of)

  def test_stage2_stage1(self):
    # Add a duplicate to stage1 to make sure it is copied
    self.stage1_conformer.duplicate_of.append(999)
    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.stage2_conformer, self.stage1_conformer)
    self.assertIsNone(got_conflict)
    self.assertEqual(got_conf.duplicate_of, [999])
    # Just check a random field that is in stage2 but not stage1
    self.assertNotEmpty(got_conf.properties.normal_modes)

  def test_stage2_stage1_conflict_energy(self):
    self.stage2_conformer.properties.initial_geometry_energy.value = -1.23
    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.stage2_conformer, self.stage1_conformer)
    self.assertEqual(got_conflict, [
        618451001,
        1, 1, 1, 1, -406.51179, 0.052254, -406.522079, 2.5e-05, True, True,
        1, 1, 1, 1, -1.23, 0.052254, -406.522079, 2.5e-05, True, True
    ])
    # Just check a random field that is in stage2 but not stage1
    self.assertNotEmpty(got_conf.properties.normal_modes)
    # This stage2 values should be returned
    self.assertEqual(got_conf.properties.initial_geometry_energy.value, -1.23)

  def test_stage2_stage1_conflict_error_codes(self):
    self.stage2_conformer.properties.errors.error_nstat1 = 999
    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.stage2_conformer, self.stage1_conformer)
    self.assertEqual(got_conflict, [
        618451001,
        1, 1, 1, 1, -406.51179, 0.052254, -406.522079, 2.5e-05, True, True,
        999, 1, 1, 1, -406.51179, 0.052254, -406.522079, 2.5e-05, True, True
    ])
    # Just check a random field that is in stage2 but not stage1
    self.assertNotEmpty(got_conf.properties.normal_modes)

  def test_stage2_stage1_conflict_missing_geometry(self):
    self.stage2_conformer.ClearField('optimized_geometry')
    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.stage2_conformer, self.stage1_conformer)
    self.assertEqual(got_conflict, [
        618451001,
        1, 1, 1, 1, -406.51179, 0.052254, -406.522079, 2.5e-05, True, True,
        1, 1, 1, 1, -406.51179, 0.052254, -406.522079, 2.5e-05, True, False
    ])
    # Just check a random field that is in stage2 but not stage1
    self.assertNotEmpty(got_conf.properties.normal_modes)

  def test_stage2_stage1_no_conflict_minus1(self):
    # If stage2 contains a -1, we keep that (stricter error checking later on)
    self.stage2_conformer.properties.initial_geometry_energy.value = -1.0
    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.stage2_conformer, self.stage1_conformer)
    self.assertIsNone(got_conflict)
    self.assertEqual(got_conf.properties.initial_geometry_energy.value, -1.0)

  def test_stage2_stage1_no_conflict_approx_equal(self):
    self.stage2_conformer.properties.initial_geometry_energy.value += 1e-7
    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.stage2_conformer, self.stage1_conformer)
    self.assertIsNone(got_conflict)
    # Just check a random field from stage2
    self.assertNotEmpty(got_conf.properties.normal_modes)

  def test_stage2_duplicate(self):
    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.stage2_conformer, self.duplicate_conformer)
    self.assertIsNone(got_conflict)
    self.assertEqual(got_conf.duplicate_of, [111, 222])
    self.assertEqual(got_conf.duplicated_by, 123)
    # Just check a random field from stage2
    self.assertNotEmpty(got_conf.properties.normal_modes)

  def test_stage1_duplicate(self):
    got_conf, got_conflict = smu_utils_lib.merge_conformer(
        self.stage1_conformer, self.duplicate_conformer)
    self.assertIsNone(got_conflict)
    self.assertEqual(got_conf.duplicate_of, [111, 222])
    self.assertEqual(got_conf.duplicated_by, 123)
    # Just check a random field from stage1
    self.assertTrue(got_conf.properties.HasField('initial_geometry_energy'))

  def test_multiple_initial_geometries(self):
    bad_conformer = copy.deepcopy(self.stage1_conformer)
    bad_conformer.initial_geometries.append(bad_conformer.initial_geometries[0])
    with self.assertRaises(ValueError):
      smu_utils_lib.merge_conformer(bad_conformer, self.stage2_conformer)
    with self.assertRaises(ValueError):
      smu_utils_lib.merge_conformer(self.stage2_conformer, bad_conformer)

  def test_multiple_bond_topologies(self):
    bad_conformer = copy.deepcopy(self.stage1_conformer)
    bad_conformer.bond_topologies.append(bad_conformer.bond_topologies[0])
    with self.assertRaises(ValueError):
      smu_utils_lib.merge_conformer(bad_conformer, self.stage2_conformer)
    with self.assertRaises(ValueError):
      smu_utils_lib.merge_conformer(self.stage2_conformer, bad_conformer)

  def test_different_bond_topologies(self):
    self.stage1_conformer.bond_topologies[0].atoms[0] = (
        dataset_pb2.BondTopology.ATOM_H)
    with self.assertRaises(ValueError):
      smu_utils_lib.merge_conformer(self.stage1_conformer,
                                    self.stage2_conformer)
    with self.assertRaises(ValueError):
      smu_utils_lib.merge_conformer(self.stage2_conformer,
                                    self.stage1_conformer)


class ConformerErrorTest(absltest.TestCase):

  def test_stage1_no_error(self):
    conformer = get_stage1_conformer()
    self.assertFalse(smu_utils_lib.conformer_has_calculation_errors(conformer))

  def test_stage1_error(self):
    conformer = get_stage2_conformer()
    conformer.properties.errors.error_frequencies = 123
    self.assertTrue(smu_utils_lib.conformer_has_calculation_errors(conformer))

  def test_stage2_no_error(self):
    conformer = get_stage2_conformer()
    self.assertFalse(smu_utils_lib.conformer_has_calculation_errors(conformer))

  def test_stage2_error_in_1_expected_field(self):
    conformer = get_stage2_conformer()
    conformer.properties.errors.error_rotational_modes = 123
    self.assertTrue(smu_utils_lib.conformer_has_calculation_errors(conformer))

  def test_stage2_error_in_0_expected_field(self):
    conformer = get_stage2_conformer()
    # This field is 0 to indicate no error. Why the discrepancy? Who knows!
    conformer.properties.errors.error_nsvg09 = 1
    self.assertTrue(smu_utils_lib.conformer_has_calculation_errors(conformer))

  def test_stage2_nstat1_is_3(self):
    # This is the other bizaare case. nstat1 of 3 is still considered success.
    conformer = get_stage2_conformer()
    conformer.properties.errors.error_nstat1 = 3
    self.assertFalse(smu_utils_lib.conformer_has_calculation_errors(conformer))


class FilterConformerByAvailabilityTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.conformer = dataset_pb2.Conformer()
    properties = self.conformer.properties
    # A STANDARD field
    properties.single_point_energy_pbe0d3_6_311gd.value = 1.23
    # A COMPLETE field
    properties.homo_pbe0_aug_pc_1.value = 1.23
    # An INTERNAL_ONLY field
    properties.nuclear_repulsion_energy.value = 1.23

  def test_standard(self):
    smu_utils_lib.filter_conformer_by_availability(self.conformer,
                                                   [dataset_pb2.STANDARD])
    self.assertTrue(
        self.conformer.properties.HasField(
            'single_point_energy_pbe0d3_6_311gd'))
    self.assertFalse(self.conformer.properties.HasField('homo_pbe0_aug_pc_1'))
    self.assertFalse(
        self.conformer.properties.HasField('nuclear_repulsion_energy'))

  def test_complete_and_internal_only(self):
    smu_utils_lib.filter_conformer_by_availability(
        self.conformer, [dataset_pb2.COMPLETE, dataset_pb2.INTERNAL_ONLY])
    self.assertFalse(
        self.conformer.properties.HasField(
            'single_point_energy_pbe0d3_6_311gd'))
    self.assertTrue(self.conformer.properties.HasField('homo_pbe0_aug_pc_1'))
    self.assertTrue(
        self.conformer.properties.HasField('nuclear_repulsion_energy'))


class ConformerToStandardTest(absltest.TestCase):

  def setUp(self):
    super().setUp()

    self.conformer = get_stage2_conformer()

  def test_field_filtering(self):
    # Check that the field which should be filtered starts out set
    self.assertTrue(self.conformer.properties.HasField(
        'single_point_energy_hf_6_31gd'))

    got = smu_utils_lib.conformer_to_standard(self.conformer)
    # Check for a field that was originally in self.conformer and should be
    # filtered and a field which should still be present.
    self.assertTrue(got.properties.HasField(
        'single_point_energy_pbe0d3_6_311gd'))
    self.assertFalse(
        got.properties.HasField('single_point_energy_hf_6_31gd'))

  def test_remove_error_conformer(self):
    self.conformer.properties.errors.error_frequencies = 123

    self.assertIsNone(smu_utils_lib.conformer_to_standard(self.conformer))

  def test_remove_duplicate(self):
    self.conformer.duplicated_by = 123

    self.assertIsNone(smu_utils_lib.conformer_to_standard(self.conformer))


class DetermineFateTest(parameterized.TestCase):

  def test_duplicate_same_topology(self):
    conformer = get_stage1_conformer()
    # bond topology is conformer_id // 1000
    conformer.duplicated_by = conformer.conformer_id + 1
    self.assertEqual(dataset_pb2.Conformer.FATE_DUPLICATE_SAME_TOPOLOGY,
                     smu_utils_lib.determine_fate(conformer))

  def test_duplicate_different_topology(self):
    conformer = get_stage1_conformer()
    # bond topology is conformer_id // 1000
    conformer.duplicated_by = conformer.conformer_id + 1000
    self.assertEqual(dataset_pb2.Conformer.FATE_DUPLICATE_DIFFERENT_TOPOLOGY,
                     smu_utils_lib.determine_fate(conformer))

  @parameterized.parameters(
      (2, dataset_pb2.Conformer.FATE_GEOMETRY_OPTIMIZATION_PROBLEM),
      (5, dataset_pb2.Conformer.FATE_DISASSOCIATED),
      (4, dataset_pb2.Conformer.FATE_FORCE_CONSTANT_FAILURE),
      (6, dataset_pb2.Conformer.FATE_DISCARDED_OTHER))
  def test_geometry_failures(self, nstat1, expected_fate):
    conformer = get_stage1_conformer()
    conformer.properties.errors.error_nstat1 = nstat1
    self.assertEqual(expected_fate, smu_utils_lib.determine_fate(conformer))

  def test_no_result(self):
    conformer = get_stage1_conformer()
    self.assertEqual(dataset_pb2.Conformer.FATE_NO_CALCULATION_RESULTS,
                     smu_utils_lib.determine_fate(conformer))

  def test_calculation_errors(self):
    conformer = get_stage2_conformer()
    # This is a random choice of an error to set. I just need some error.
    conformer.properties.errors.error_atomic_analysis = 999
    self.assertEqual(dataset_pb2.Conformer.FATE_CALCULATION_WITH_ERROR,
                     smu_utils_lib.determine_fate(conformer))

  def test_success(self):
    conformer = get_stage2_conformer()
    self.assertEqual(dataset_pb2.Conformer.FATE_SUCCESS,
                     smu_utils_lib.determine_fate(conformer))


class ToBondTopologySummaryTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.conformer = get_stage2_conformer()

  def test_dup_same(self):
    self.conformer.fate = dataset_pb2.Conformer.FATE_DUPLICATE_SAME_TOPOLOGY
    got = list(
        smu_utils_lib.conformer_to_bond_topology_summaries(self.conformer))
    self.assertLen(got, 1)
    self.assertEqual(got[0].bond_topology.bond_topology_id,
                     self.conformer.bond_topologies[0].bond_topology_id)
    self.assertEqual(got[0].count_attempted_conformers, 1)
    self.assertEqual(got[0].count_duplicates_same_topology, 1)

  def test_dup_diff(self):
    self.conformer.fate = (
        dataset_pb2.Conformer.FATE_DUPLICATE_DIFFERENT_TOPOLOGY)
    got = list(
        smu_utils_lib.conformer_to_bond_topology_summaries(self.conformer))
    self.assertLen(got, 1)
    self.assertEqual(got[0].count_attempted_conformers, 1)
    self.assertEqual(got[0].count_duplicates_different_topology, 1)

  def test_geometry_failed(self):
    self.conformer.fate = (dataset_pb2.Conformer.FATE_DISCARDED_OTHER)
    got = list(
        smu_utils_lib.conformer_to_bond_topology_summaries(self.conformer))
    self.assertLen(got, 1)
    self.assertEqual(got[0].count_attempted_conformers, 1)
    self.assertEqual(got[0].count_failed_geometry_optimization, 1)

  def test_missing_calculation(self):
    self.conformer.fate = dataset_pb2.Conformer.FATE_NO_CALCULATION_RESULTS
    got = list(
        smu_utils_lib.conformer_to_bond_topology_summaries(self.conformer))
    self.assertLen(got, 1)
    self.assertEqual(got[0].count_attempted_conformers, 1)
    self.assertEqual(got[0].count_kept_geometry, 1)
    self.assertEqual(got[0].count_missing_calculation, 1)

  def test_calculation_with_error(self):
    self.conformer.fate = dataset_pb2.Conformer.FATE_CALCULATION_WITH_ERROR
    self.conformer.bond_topologies.append(self.conformer.bond_topologies[0])
    self.conformer.bond_topologies[-1].bond_topology_id = 123
    got = list(
        smu_utils_lib.conformer_to_bond_topology_summaries(self.conformer))
    self.assertLen(got, 2)
    # We don't actually care about the order, but this is what comes out right
    # now.
    self.assertEqual(got[0].bond_topology.bond_topology_id, 123)
    self.assertEqual(got[0].count_attempted_conformers, 0)
    self.assertEqual(got[0].count_kept_geometry, 0)
    self.assertEqual(got[0].count_calculation_with_error, 0)
    self.assertEqual(got[0].count_detected_match_with_error, 1)

    self.assertEqual(got[1].bond_topology.bond_topology_id,
                     self.conformer.bond_topologies[0].bond_topology_id)
    self.assertEqual(got[1].count_attempted_conformers, 1)
    self.assertEqual(got[1].count_kept_geometry, 1)
    self.assertEqual(got[1].count_calculation_with_error, 1)
    self.assertEqual(got[1].count_detected_match_with_error, 0)

  def test_calculation_success(self):
    self.conformer.fate = dataset_pb2.Conformer.FATE_SUCCESS
    self.conformer.bond_topologies.append(self.conformer.bond_topologies[0])
    self.conformer.bond_topologies[-1].bond_topology_id = 123
    got = list(
        smu_utils_lib.conformer_to_bond_topology_summaries(self.conformer))
    self.assertLen(got, 2)
    # We don't actually care about the order, but this is what comes out right
    # now.
    self.assertEqual(got[0].bond_topology.bond_topology_id, 123)
    self.assertEqual(got[0].count_attempted_conformers, 0)
    self.assertEqual(got[0].count_kept_geometry, 0)
    self.assertEqual(got[0].count_calculation_success, 0)
    self.assertEqual(got[0].count_detected_match_success, 1)

    self.assertEqual(got[1].bond_topology.bond_topology_id,
                     self.conformer.bond_topologies[0].bond_topology_id)
    self.assertEqual(got[1].count_attempted_conformers, 1)
    self.assertEqual(got[1].count_kept_geometry, 1)
    self.assertEqual(got[1].count_calculation_success, 1)
    self.assertEqual(got[1].count_detected_match_success, 0)


class LabeledSmilesTester(absltest.TestCase):

  def test_atom_labels(self):
    mol = Chem.MolFromSmiles('FCON[NH2+][O-]', sanitize=False)
    self.assertIsNotNone(mol)
    smiles_before = Chem.MolToSmiles(mol)
    self.assertEqual(
        smu_utils_lib.labeled_smiles(mol), 'F[CH2:1][O:2][NH:3][NH2+:4][O-:5]')
    # Testing both the atom numbers and the smiles is redundant,
    # but guards against possible future changes.
    for atom in mol.GetAtoms():
      self.assertEqual(atom.GetAtomMapNum(), 0)
    self.assertEqual(Chem.MolToSmiles(mol), smiles_before)


if __name__ == '__main__':
  absltest.main()
