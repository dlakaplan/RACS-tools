#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Note: To use the 'upload' functionality of this file, you must:
#   $ pipenv install twine --dev

import io
import os
import sys
from shutil import rmtree

from setuptools import find_packages, Command  # setup,
from numpy.distutils.core import Extension, setup


# Package meta-data.
NAME = 'RACS-tools'
DESCRIPTION = 'Useful scripts for RACS.'
URL = 'https://github.com/AlecThomson/RACS-tools'
EMAIL = 'alec.thomson@csiro.au'
AUTHOR = 'Alec Thomson'
REQUIRES_PYTHON = '>=3.6.0'
VERSION = '1.0.5'

# What packages are required for this module to be executed?
REQUIRED = [
    'astropy', 'radio_beam', 'schwimmbad', 'psutil',
    'scipy', 'numpy', 'tqdm', 'spectral_cube',
]

# What packages are optional?
EXTRAS = {
    'mpi': ['mpi4py'],
    # 'fancy feature': ['django'],
}

lib = Extension(name='racs_tools.gaussft',
                sources=['racs_tools/gaussft.f'],
                extra_f90_compile_args=["-ffixed-form"]
                )

# The rest you shouldn't have to touch too much :)
# ------------------------------------------------
# Except, perhaps the License and Trove Classifiers!
# If you do change the License, remember to change the Trove Classifier for that!

here = os.path.abspath(os.path.dirname(__file__))

# Import the README and use it as the long-description.
# Note: this will only work if 'README.md' is present in your MANIFEST.in file!
try:
    with io.open(os.path.join(here, 'README.md'), encoding='utf-8') as f:
        long_description = '\n' + f.read()
except FileNotFoundError:
    long_description = DESCRIPTION

# Load the package's __version__.py module as a dictionary.
about = {}
if not VERSION:
    project_slug = NAME.lower().replace("-", "_").replace(" ", "_")
    with open(os.path.join(here, project_slug, '__version__.py')) as f:
        exec(f.read(), about)
else:
    about['__version__'] = VERSION


class UploadCommand(Command):
    """Support setup.py upload."""

    description = 'Build and publish the package.'
    user_options = []

    @staticmethod
    def status(s):
        """Prints things in bold."""
        print('\033[1m{0}\033[0m'.format(s))

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        try:
            self.status('Removing previous builds…')
            rmtree(os.path.join(here, 'dist'))
        except OSError:
            pass

        self.status('Building Source and Wheel (universal) distribution…')
        os.system(
            '{0} setup.py sdist bdist_wheel --universal'.format(sys.executable))

        self.status('Uploading the package to PyPI via Twine…')
        os.system('twine upload dist/*')

        self.status('Pushing git tags…')
        os.system('git tag v{0}'.format(about['__version__']))
        os.system('git push --tags')

        sys.exit()


# Where the magic happens:
setup(
    name=NAME,
    version=about['__version__'],
    description=DESCRIPTION,
    long_description=long_description,
    long_description_content_type='text/markdown',
    author=AUTHOR,
    author_email=EMAIL,
    python_requires=REQUIRES_PYTHON,
    url=URL,
    packages=find_packages(
        exclude=["tests", "*.tests", "*.tests.*", "tests.*"]),
    # packages=['racs_tools'],
    # If your package is a single module, use this instead of 'packages':
    # py_modules=['racs_tools'],

    entry_points={
        'console_scripts': [
            'beamcon_2D=racs_tools.beamcon_2D:cli',
            'beamcon_3D=racs_tools.beamcon_3D:cli',
            'getnoise_list=racs_tools.getnoise_list:cli'
        ],
    },
    install_requires=REQUIRED,
    extras_require=EXTRAS,
    ext_modules=[lib],
    include_package_data=True,
    license='BSD',
    classifiers=[
        # Trove classifiers
        # Full list: https://pypi.python.org/pypi?%3Aaction=list_classifiers
        'License :: OSI Approved :: BSD License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python :: Implementation :: PyPy'
    ],
    # $ setup.py publish support.
    cmdclass={
        'upload': UploadCommand,
    },
)
