from __future__ import print_function

import sys
from types import ModuleType
from inspect import getsourcelines
from contextlib import contextmanager

from tqdm import tqdm

from .._util import Capture, DummyBar, NotebookWrapper
from ..error import Error, Missing, MultipleFragments, DuplicatedDescriptorName
from .context import Context
from .descriptor import Descriptor, MissingValueException, is_descriptor_class


class Calculator(object):
    r"""descriptor calculator.

    Parameters:
        descs: see Calculator.register() method
        ignore_3D: see Calculator.register() method
    """

    __slots__ = (
        '_descriptors', '_name_dict', '_explicit_hydrogens', '_kekulizes', '_require_3D',
        '_cache', '_debug', '_progress_bar'
    )

    def __setstate__(self, dict):
        ds = self._descriptors = dict.get('_descriptors', [])
        self._name_dict = {str(d): d for d in ds}
        self._explicit_hydrogens = dict.get('_explicit_hydrogens', set([True, False]))
        self._kekulizes = dict.get('_kekulizes', set([True, False]))
        self._require_3D = dict.get('_require_3D', False)

    def __reduce_ex__(self, version):
        return self.__class__, (), {
            '_descriptors': self._descriptors,
            '_explicit_hydrogens': self._explicit_hydrogens,
            '_kekulizes': self._kekulizes,
            '_require_3D': self._require_3D,
        }

    def __getitem__(self, key):
        return self._name_dict[key]

    def __init__(self, descs=[], ignore_3D=False):
        self._descriptors = []
        self._name_dict = {}

        self._explicit_hydrogens = set()
        self._kekulizes = set()
        self._require_3D = False
        self._debug = False

        self.register(descs, ignore_3D=ignore_3D)

    @property
    def descriptors(self):
        r'''all descriptors.

        you can get/set/delete descriptor.

        Returns:
            tuple[Descriptor]: registered descriptors
        '''
        return tuple(self._descriptors)

    @descriptors.setter
    def descriptors(self, descs):
        del self.descriptors
        self.register(descs)

    @descriptors.deleter
    def descriptors(self):
        self._descriptors = []
        self._name_dict = {}
        self._explicit_hydrogens.clear()
        self._kekulizes.clear()
        self._require_3D = False

    def __len__(self):
        return len(self._descriptors)

    def _register_one(self, desc, check_only=False, ignore_3D=False):
        if not isinstance(desc, Descriptor):
            raise ValueError('{!r} is not descriptor'.format(desc))

        if ignore_3D and desc.require_3D:
            return

        self._explicit_hydrogens.add(bool(desc.explicit_hydrogens))
        self._kekulizes.add(bool(desc.kekulize))
        self._require_3D |= desc.require_3D

        for dep in (desc.dependencies() or {}).values():
            if isinstance(dep, Descriptor):
                self._register_one(dep, check_only=True)

        if not check_only:
            sdesc = str(desc)
            old = self._name_dict.get(sdesc)
            if old is not None:
                raise DuplicatedDescriptorName(desc, old)

            self._name_dict[sdesc] = desc
            self._descriptors.append(desc)

    def register(self, desc, ignore_3D=False):
        r"""register descriptors.

        Descriptor-like:
            * Descriptor instance: self
            * Descriptor class: use Descriptor.preset() method
            * module: use Descriptor-likes in module
            * Iterable: use Descriptor-likes in Iterable

        Parameters:
            desc(Descriptor-like): descriptors to register
            ignore_3D(bool): ignore 3D descriptors
        """
        if not hasattr(desc, '__iter__'):
            if is_descriptor_class(desc):
                for d in desc.preset():
                    self._register_one(d, ignore_3D=ignore_3D)

            elif isinstance(desc, ModuleType):
                self.register(get_descriptors_from_module(desc, True), ignore_3D=ignore_3D)

            else:
                self._register_one(desc, ignore_3D=ignore_3D)

        else:
            for d in desc:
                self.register(d, ignore_3D=ignore_3D)

    def _calculate_one(self, cxt, desc, reset):
        if desc in self._cache:
            return self._cache[desc]

        if reset:
            cxt.reset()
        desc._context = cxt
        cxt.add_stack(desc)

        if desc.require_connected and desc._context.n_frags != 1:
            desc.fail(MultipleFragments())

        args = {
            name: self._calculate_one(cxt, dep, False)
            if dep is not None else None
            for name, dep in (desc.dependencies() or {}).items()
        }

        r = desc.calculate(**args)

        if self._debug:
            self._check_rtype(desc, r)

        self._cache[desc] = r

        return r

    def _check_rtype(self, desc, result):
        if desc.rtype is None:
            return

        if isinstance(result, Error):
            return

        if not isinstance(result, desc.rtype):
            raise TypeError('{} not match {}'.format(result, desc.rtype))

    def _calculate(self, cxt):
        self._cache = {}
        for desc in self.descriptors:
            try:
                yield self._calculate_one(cxt, desc, True)
            except MissingValueException as e:
                yield Missing(e.error, desc._context.get_stack())
            except Exception as e:
                yield Error(e, desc._context.get_stack())
            finally:
                if hasattr(desc, '_context'):
                    del desc._context

    def __call__(self, mol, id=-1):
        r"""calculate descriptors.

        :type mol: rdkit.Chem.Mol
        :param mol: molecular

        :type id: int
        :param id: conformer id

        :rtype: [scalar or Error]
        :returns: iterator of descriptor and value
        """
        return list(self._calculate(Context.from_calculator(self, mol, id)))

    def _serial(self, mols, nmols, quiet, ipynb, id):
        with self._progress(quiet, nmols, ipynb) as bar:
            for m in mols:
                with Capture() as capture:
                    r = list(self._calculate(Context.from_calculator(self, m, id)))

                for e in capture.result:
                    e = e.rstrip()
                    if not e:
                        continue

                    bar.write(e, file=capture.orig)

                yield r
                bar.update()

    @contextmanager
    def _progress(self, quiet, total, ipynb):
        args = {
            'dynamic_ncols': True,
            'leave': True,
            'total': total
        }

        if quiet:
            Bar = DummyBar
        elif ipynb:
            Bar = NotebookWrapper
        else:
            Bar = tqdm

        try:
            with Bar(**args) as self._progress_bar:
                yield self._progress_bar
        finally:
            if hasattr(self, '_progress_bar'):
                del self._progress_bar

    def echo(self, s, file=sys.stdout, end='\n'):
        '''output message

        Parameters:
            s(str): message to output
            file(file-like): output to
            end(str): end mark of message

        Return:
            None
        '''
        p = getattr(self, '_progress_bar', None)
        if p is not None:
            p.write(s, file=file, end='\n')
            return

        print(s, file=file, end='\n')

    def map(self, mols, nproc=None, nmols=None, quiet=False, ipynb=False, id=-1):
        r"""calculate descriptors over mols.

        Parameters:
            mols(Iterable[rdkit.Mol]): moleculars

            nproc(int): number of process to use. default: multiprocessing.cpu_count()

            nmols(int): number of all mols to use in progress-bar. default: mols.__len__()

            quiet(bool): don't show progress bar. default: False

            ipynb(bool): use ipython notebook progress bar. default: False

            id(int): conformer id to use. default: -1.

        Returns:
            Iterator[scalar]
        """

        if hasattr(mols, '__len__'):
            nmols = len(mols)

        if nproc == 1:
            return self._serial(mols, nmols=nmols, quiet=quiet, ipynb=ipynb, id=id)
        else:
            return self._parallel(mols, nproc, nmols=nmols, quiet=quiet, ipynb=ipynb, id=id)

    def pandas(self, mols, nproc=None, nmols=None, quiet=False, ipynb=False, id=-1):
        r"""calculate descriptors over mols.

        Returns:
            pandas.DataFrame
        """
        import pandas

        return pandas.DataFrame(
            self.map(mols, nproc, nmols, quiet, ipynb, id),
            columns=[str(d) for d in self.descriptors]
        )


def get_descriptors_from_module(mdl, submodule=False):
    r"""get descriptors from module.

    Parameters:
        mdl(module): module to search

    Returns:
        [Descriptor]
    """

    __all__ = getattr(mdl, '__all__', None)
    if __all__ is None:
        __all__ = dir(mdl)

    if submodule:
        def check(fn):
            return is_descriptor_class(fn) or isinstance(fn, ModuleType)
    else:
        def check(fn):
            return is_descriptor_class(fn)

    descs = [
        fn
        for fn in (getattr(mdl, name) for name in __all__ if name[:1] != '_')
        if check(fn)
    ]

    def key_by_def(d):
        try:
            return getsourcelines(d)[1]
        except IOError:
            return sys.maxsize

    descs.sort(key=key_by_def)
    return descs
