from tempfile import mkdtemp
from shutil import rmtree
import sys
import six
import subprocess
import numpy as np
import re
from oddt import toolkit


class autodock_vina(object):
    def __init__(self,
                 protein=None,
                 auto_ligand=None,
                 size=(10, 10, 10),
                 center=(0, 0, 0),
                 exhaustiveness=8,
                 num_modes=9,
                 energy_range=3,
                 seed=None,
                 prefix_dir='/tmp',
                 n_cpu=1,
                 executable=None,
                 autocleanup=True,
                 skip_bad_mols=True):
        """Autodock Vina docking engine, which extends it's capabilities:
        automatic box (auto-centering on ligand).

        Parameters
        ----------
            protein: oddt.toolkit.Molecule object (default=None)
                Protein object to be used while generating descriptors.

            auto_ligand: oddt.toolkit.Molecule object or string (default=None)
                Ligand use to center the docking box. Either ODDT molecule or
                a file (opened based on extesion and read to ODDT molecule).
                Box is centered on geometric center of molecule.

            size: tuple, shape=[3] (default=(10,10,10))
                Dimentions of docking box (in Angstroms)

            center: tuple, shape=[3] (default=(0,0,0))
                The center of docking box in cartesian space.

            exhaustiveness: int (default=8)
                Exhaustiveness parameter of Autodock Vina

            num_modes: int (default=9)
                Number of conformations generated by Autodock Vina

            energy_range: int (default=3)
                Energy range cutoff for Autodock Vina

            seed: int or None (default=None)
                Random seed for Autodock Vina

            prefix_dir: string (default=/tmp)
                Temporary directory for Autodock Vina files

            executable: string or None (default=None)
                Autodock Vina executable location in the system.
                It's realy necessary if autodetection fails.

            autocleanup: bool (default=True)
                Should the docking engine clean up after execution?

            skip_bad_mols: bool (default=True)
                Should molecules that crash Autodock Vina be skipped.
        """
        self.dir = prefix_dir
        self._tmp_dir = None
        # define binding site
        self.size = size
        self.center = center
        # center automaticaly on ligand
        if auto_ligand:
            if type(auto_ligand) is str:
                extension = auto_ligand.split('.')[-1]
                auto_ligand = six.next(toolkit.readfile(extension, auto_ligand))
            self.center = tuple(np.array([atom.coords for atom in auto_ligand],
                                         dtype=np.float32).mean(axis=0))
        # autodetect Vina executable
        if not executable:
            try:
                self.executable = (subprocess.check_output(['which', 'vina'])
                                   .decode('ascii').split('\n')[0])
            except subprocess.CalledProcessError:
                raise Exception('Could not find Autodock Vina binary.'
                                'You have to install it globaly or supply binary'
                                'full directory via `executable` parameter.')
        else:
            self.executable = executable
        # detect version
        self.version = (subprocess.check_output([self.executable, '--version'])
                        .decode('ascii').split(' ')[2])
        self.autocleanup = autocleanup
        self.cleanup_dirs = set()

        # share protein to class
        self.protein = None
        self.protein_file = None
        if protein:
            self.set_protein(protein)
        self.skip_bad_mols = skip_bad_mols

        # pregenerate common Vina parameters
        self.params = []
        self.params += ['--center_x', str(self.center[0]),
                        '--center_y', str(self.center[1]),
                        '--center_z', str(self.center[2])]
        self.params += ['--size_x', str(self.size[0]),
                        '--size_y', str(self.size[1]),
                        '--size_z', str(self.size[2])]
        if n_cpu > 0:
            self.params += ['--cpu', str(n_cpu)]
        self.params += ['--exhaustiveness', str(exhaustiveness)]
        if seed is not None:
            self.params += ['--seed', str(seed)]
        self.params += ['--num_modes', str(num_modes)]
        self.params += ['--energy_range', str(energy_range)]

    @property
    def tmp_dir(self):
        if not self._tmp_dir:
            self._tmp_dir = mkdtemp(dir=self.dir, prefix='autodock_vina_')
            self.cleanup_dirs.add(self._tmp_dir)
        return self._tmp_dir

    @tmp_dir.setter
    def tmp_dir(self, value):
        self._tmp_dir = value

    def set_protein(self, protein):
        """Change protein to dock to.

        Parameters
        ----------
            protein: oddt.toolkit.Molecule object
                Protein object to be used.
        """
        # generate new directory
        self._tmp_dir = None
        if protein:
            self.protein = protein
            if type(protein) is str:
                extension = protein.split('.')[-1]
                if extension == 'pdbqt':
                    self.protein_file = protein
                    self.protein = six.next(toolkit.readfile(extension, protein))
                else:
                    self.protein = six.next(toolkit.readfile(extension, protein))
                    self.protein.protein = True
                    self.protein_file = self.tmp_dir + '/protein.pdbqt'
                    self.protein.write('pdbqt', self.protein_file, opt={'r': None, 'c': None}, overwrite=True)
            else:
                # write protein to file
                self.protein_file = self.tmp_dir + '/protein.pdbqt'
                self.protein.write('pdbqt', self.protein_file, opt={'r': None, 'c': None}, overwrite=True)

    def score(self, ligands, protein=None, single=False):
        """Automated scoring procedure.

        Parameters
        ----------
            ligands: iterable of oddt.toolkit.Molecule objects
                Ligands to score

            protein: oddt.toolkit.Molecule object or None
                Protein object to be used. If None, then the default
                one is used, else the protein is new default.

            single: bool (default=False)
                A flag to indicate single ligand scoring - performance reasons
                (eg. there is no need for subdirectory for one ligand)

        Returns
        -------
            ligands : array of oddt.toolkit.Molecule objects
                Array of ligands (scores are stored in mol.data method)
        """
        if protein:
            self.set_protein(protein)
        if not self.protein_file:
            raise IOError("No receptor.")
        if single:
            ligands = [ligands]
        ligand_dir = mkdtemp(dir=self.tmp_dir, prefix='ligands_')
        output_array = []
        for n, ligand in enumerate(ligands):
            # write ligand to file
            ligand_file = ligand_dir + '/' + str(n) + '_' + re.sub('[^A-Za-z0-9]+', '_', ligand.title) + '.pdbqt'
            ligand.write('pdbqt', ligand_file, overwrite=True, opt={'b': None})
            try:
                scores = parse_vina_scoring_output(subprocess.check_output([self.executable,
                                                                            '--score_only',
                                                                            '--receptor',
                                                                            self.protein_file,
                                                                            '--ligand',
                                                                            ligand_file] + self.params,
                                                                           stderr=subprocess.STDOUT))
            except subprocess.CalledProcessError as e:
                sys.stderr.write(e.output)
                if self.skip_bad_mols:
                    continue
                else:
                    raise Exception('Autodock Vina failed. Command: "%s"' % ' '.join(e.cmd))
            ligand.data.update(scores)
            output_array.append(ligand)
        rmtree(ligand_dir)
        return output_array

    def dock(self, ligands, protein=None, single=False):
        """Automated docking procedure.

        Parameters
        ----------
            ligands: iterable of oddt.toolkit.Molecule objects
                Ligands to dock

            protein: oddt.toolkit.Molecule object or None
                Protein object to be used. If None, then the default one
                is used, else the protein is new default.

            single: bool (default=False)
                A flag to indicate single ligand docking - performance reasons
                (eg. there is no need for subdirectory for one ligand)

        Returns
        -------
            ligands : array of oddt.toolkit.Molecule objects
                Array of ligands (scores are stored in mol.data method)
        """
        if protein:
            self.set_protein(protein)
        if not self.protein_file:
            raise IOError("No receptor.")
        if single:
            ligands = [ligands]
        ligand_dir = mkdtemp(dir=self.tmp_dir, prefix='ligands_')
        output_array = []
        for n, ligand in enumerate(ligands):
            # write ligand to file
            ligand_file = ligand_dir + '/' + str(n) + '_' + re.sub('[^A-Za-z0-9]+', '_', ligand.title) + '.pdbqt'
            ligand_outfile = ligand_dir + '/' + str(n) + '_' + re.sub('[^A-Za-z0-9]+', '_', ligand.title) + '_out.pdbqt'
            ligand.write('pdbqt', ligand_file, overwrite=True, opt={'b': None})
            try:
                vina = parse_vina_docking_output(subprocess.check_output([self.executable,
                                                                          '--receptor',
                                                                          self.protein_file,
                                                                          '--ligand', ligand_file,
                                                                          '--out', ligand_outfile] + self.params,
                                                                         stderr=subprocess.STDOUT))
            except subprocess.CalledProcessError as e:
                sys.stderr.write(e.output.decode('ascii'))
                if self.skip_bad_mols:
                    continue
                else:
                    raise Exception('Autodock Vina failed. Command: "%s"' % ' '.join(e.cmd))
            # HACK # overcome connectivity problems in obabel
            source_ligand = six.next(toolkit.readfile('pdbqt', ligand_file))
            for lig, scores in zip([lig for lig in toolkit.readfile('pdbqt', ligand_outfile, opt={'b': None})], vina):
                # HACK # copy data from source
                clone = source_ligand.clone
                clone.clone_coords(lig)
                clone.data.update(scores)
                output_array.append(clone)
        rmtree(ligand_dir)
        return output_array

    def clean(self):
        for d in self.cleanup_dirs:
            rmtree(d)

    def predict_ligand(self, ligand):
        """Local method to score one ligand and update it's scores.

        Parameters
        ----------
            ligand: oddt.toolkit.Molecule object
                Ligand to be scored

        Returns
        -------
            ligand: oddt.toolkit.Molecule object
                Scored ligand with updated scores
        """
        return self.score([ligand])[0]

    def predict_ligands(self, ligands):
        """Method to score ligands lazily

        Parameters
        ----------
            ligands: iterable of oddt.toolkit.Molecule objects
                Ligands to be scored

        Returns
        -------
            ligand: iterator of oddt.toolkit.Molecule objects
                Scored ligands with updated scores
        """
        return self.score(ligands)


def parse_vina_scoring_output(output):
    """Function parsing Autodock Vina scoring output to a dictionary

    Parameters
    ----------
        output : string
            Autodock Vina standard ouptud (STDOUT).

    Returns
    -------
        out : dict
            dicitionary containing scores computed by Autodock Vina
    """
    out = {}
    r = re.compile('^(Affinity:|\s{4})')
    for line in output.decode('ascii').split('\n')[13:]:  # skip some output
        if r.match(line):
            m = line.replace(' ', '').split(':')
            if m[0] == 'Affinity':
                m[1] = m[1].replace('(kcal/mol)', '')
            out['vina_' + m[0].lower()] = float(m[1])
    return out


def parse_vina_docking_output(output):
    """Function parsing Autodock Vina docking output to a dictionary

    Parameters
    ----------
        output : string
            Autodock Vina standard ouptud (STDOUT).

    Returns
    -------
        out : dict
            dicitionary containing scores computed by Autodock Vina
    """
    out = []
    r = re.compile('^\s+\d\s+')
    for line in output.decode('ascii').split('\n')[13:]:  # skip some output
        if r.match(line):
            s = line.split()
            out.append({'vina_affinity': s[1], 'vina_rmsd_lb': s[2], 'vina_rmsd_ub': s[3]})
    return out
