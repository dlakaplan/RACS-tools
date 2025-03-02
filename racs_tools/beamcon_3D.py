#!/usr/bin/env python
""" Convolve ASKAP cubes to common resolution """
__author__ = "Alec Thomson"

from racs_tools.beamcon_2D import my_ceil, round_up
from spectral_cube.utils import SpectralCubeWarning
import warnings
from astropy.utils.exceptions import AstropyWarning
from astropy.convolution import convolve, convolve_fft
from racs_tools import convolve_uv
import os
import stat
import sys
import numpy as np
import scipy.signal
from astropy import units as u
from astropy.io import fits, ascii
from astropy.table import Table
from spectral_cube import SpectralCube
from radio_beam import Beam, Beams
from radio_beam.utils import BeamError
from tqdm import tqdm, trange
from racs_tools import au2
import functools
import psutil
try:
    from mpi4py import MPI
    mpiSwitch = True
except:
    mpiSwitch = False

# Fail if script has been started with mpiexec & mpi4py is not installed
if os.environ.get('OMPI_COMM_WORLD_SIZE') is not None:
    if not mpiSwitch:
        print("Script called with mpiexec, but mpi4py not installed")
        sys.exit()

# Get the processing environment
if mpiSwitch:
    comm = MPI.COMM_WORLD
    nPE = comm.Get_size()
    myPE = comm.Get_rank()
else:
    nPE = 1
    myPE = 0

print = functools.partial(print, f'[{myPE}]', flush=True)

warnings.filterwarnings(action='ignore', category=SpectralCubeWarning,
                        append=True)
warnings.simplefilter('ignore', category=AstropyWarning)

#############################################
#### ADAPTED FROM SCRIPT BY T. VERNSTROM ####
#############################################


class Error(OSError):
    pass


class SameFileError(Error):
    """Raised when source and destination are the same file."""


class SpecialFileError(OSError):
    """Raised when trying to do a kind of operation (e.g. copying) which is
    not supported on a special file (e.g. a named pipe)"""


class ExecError(OSError):
    """Raised when a command could not be executed"""


class ReadError(OSError):
    """Raised when an archive cannot be read"""


class RegistryError(Exception):
    """Raised when a registry operation with the archiving
    and unpacking registeries fails"""


def _samefile(src, dst):
    # Macintosh, Unix.
    if hasattr(os.path, 'samefile'):
        try:
            return os.path.samefile(src, dst)
        except OSError:
            return False


def copyfile(src, dst, *, follow_symlinks=True, verbose=True):
    """Copy data from src to dst.

    If follow_symlinks is not set and src is a symbolic link, a new
    symlink will be created instead of copying the file it points to.

    """
    if _samefile(src, dst):
        raise SameFileError("{!r} and {!r} are the same file".format(src, dst))

    for fn in [src, dst]:
        try:
            st = os.stat(fn)
        except OSError:
            # File most likely does not exist
            pass
        else:
            # XXX What about other special files? (sockets, devices...)
            if stat.S_ISFIFO(st.st_mode):
                raise SpecialFileError("`%s` is a named pipe" % fn)

    if not follow_symlinks and os.path.islink(src):
        os.symlink(os.readlink(src), dst)
    else:
        with open(src, 'rb') as fsrc:
            with open(dst, 'wb') as fdst:
                copyfileobj(fsrc, fdst, verbose=verbose)
    return dst


def copyfileobj(fsrc, fdst, length=16*1024, verbose=True):
    #copied = 0
    total = os.fstat(fsrc.fileno()).st_size
    with tqdm(
            total=total,
            disable=(not verbose),
            unit_scale=True,
            desc='Copying file'
    ) as pbar:
        while True:
            buf = fsrc.read(length)
            if not buf:
                break
            fdst.write(buf)
            copied = len(buf)
            pbar.update(copied)


def getbeams(beamlog, verbose=False):
    """

    colnames=['Channel', 'BMAJarcsec', 'BMINarcsec', 'BPAdeg']
    """
    # Get beamlog
    if verbose:
        print(f'Getting beams from {beamlog}')

    beams = Table.read(beamlog, format='ascii.commented_header')
    for col in beams.colnames:
        idx = col.find('[')
        if idx == -1:
            new_col = col
            unit = u.Unit('')
        else:
            new_col = col[:idx]
            unit = u.Unit(col[idx+1:-1])
        beams[col].unit = unit
        beams[col].name = new_col
    nchan = len(beams)
    return beams, nchan


def getfacs(datadict, convbeams, verbose=False):
    """Get smoothing unit factor

    Args:
        datadict (dict): Dict of input data
        convbeams (Beams): Convolving beams
        verbose (bool, optional): Verbose output. Defaults to False.

    Returns:
        list: factors to keep units of Jy/beam after convolution
    """    """Get beam info
    
    """
    facs = []
    for conbm, oldbeam in zip(convbeams, datadict['beams']):
        fac, amp, outbmaj, outbmin, outbpa = au2.gauss_factor(
            [
                conbm.major.to(u.arcsec).value,
                conbm.minor.to(u.arcsec).value,
                conbm.pa.to(u.deg).value
            ],
            beamOrig=[
                oldbeam.major.to(u.arcsec).value,
                oldbeam.minor.to(u.arcsec).value,
                oldbeam.pa.to(u.deg).value
            ],
            dx1=datadict['dx'].to(u.arcsec).value,
            dy1=datadict['dy'].to(u.arcsec).value
        )
        facs.append(fac)
    facs = np.array(facs)
    return facs


def smooth(image, dx, dy, oldbeam, newbeam, conbeam, sfactor,
           conv_mode='robust', verbose=False):
    """smooth an image in Jy/beam

    Args:
        image (ndarray): Image plane from FITS file
        dx (Quantity): deg per pixel in image
        dy (Quantity): deg per pixel in image
        oldbeam (Beam): Original PSF
        newbeam (Beam): Target PSF
        conbeam (Beam): Convolving beam
        sfactor (float): factor to keep units in Jy/beam
        conv_mode (str): Convolution mode
        verbose (bool, optional): Verbose output. Defaults to False.

    Returns:
        ndarray: Smoothed image
    """
    if np.isnan(conbeam):
        return image*np.nan
    if np.isnan(image).all():
        return image
    else:
        # using Beams package
        if verbose:
            print(f'Using convolving beam', conbeam)
            print(f'Using scaling factor', sfactor)
        pix_scale = dy
        gauss_kern = conbeam.as_kernel(pix_scale)

        conbm1 = gauss_kern.array/gauss_kern.array.max()
        fac = sfactor
        if conv_mode == 'robust':
            newim, fac = convolve_uv.convolve(
                image.astype('f8'),
                oldbeam,
                newbeam,
                dx,
                dy,
            )
        if conv_mode == 'scipy':
            newim = scipy.signal.convolve(
                image.astype('f8'),
                conbm1,
                mode='same'
            )
        elif conv_mode == 'astropy':
            newim = convolve(
                image.astype('f8'),
                conbm1,
                normalize_kernel=False,
            )
        elif conv_mode == 'astropy_fft':
            newim = convolve_fft(
                image.astype('f8'),
                conbm1,
                normalize_kernel=False,
                allow_huge=True,
            )
    if verbose:
        print(f'Using scaling factor', fac)
    newim *= fac
    return newim


def cpu_to_use(max_cpu, count):
    """Find number of cpus to use.
    Find the right number of cpus to use when dividing up a task, such
    that there are no remainders.
    Args:
        max_cpu (int): Maximum number of cores to use for a process.
        count (float): Number of tasks.

    Returns:
        Maximum number of cores to be used that divides into the number
        of tasks (int).
    """
    factors = []
    for i in range(1, count + 1):
        if count % i == 0:
            factors.append(i)
    factors = np.array(factors)
    return max(factors[factors <= max_cpu])


def worker(idx, cubedict, conv_mode='robust', start=0):
    """parallel worker function

    Args:
        idx (int): channel index
        cubedict (dict): Datadict referring to single image cube
        conv_mode (str): Convolution mode
        start (int, optional): index to start at. Defaults to 0.

    Returns:
        ndarray: smoothed image
    """
    cube = SpectralCube.read(cubedict["filename"])
    plane = cube.unmasked_data[start+idx].value
    newim = smooth(image=plane,
                   dx=cubedict['dx'],
                   dy=cubedict['dy'],
                   oldbeam=cubedict['beams'][start+idx],
                   newbeam=cubedict['commonbeams'][start+idx],
                   conbeam=cubedict['convbeams'][start+idx],
                   sfactor=cubedict['facs'][start+idx],
                   conv_mode=conv_mode,
                   verbose=False
                   )
    return newim


def makedata(files, outdir, verbose=True):
    """init datadict

    Args:
        files (list): list of input files
        outdir (list): list of output dirs

    Returns:
        datadict: Main data dictionary
    """
    datadict = {}
    for i, (file, out) in enumerate(zip(files, outdir)):
        # Set up files
        datadict[f"cube_{i}"] = {}
        datadict[f"cube_{i}"]["filename"] = file
        datadict[f"cube_{i}"]["outdir"] = out
        # Get metadata
        header = fits.getheader(file)
        dxas = header['CDELT1']*-1*u.deg
        datadict[f"cube_{i}"]["dx"] = dxas
        dyas = header['CDELT2']*u.deg
        datadict[f"cube_{i}"]["dy"] = dyas
        if not dxas == dyas:
            raise Exception("GRID MUST BE SAME IN X AND Y")
        # Get beam info
        dirname = os.path.dirname(file)
        basename = os.path.basename(file)
        if dirname == '':
            dirname = '.'
        beamlog = f"{dirname}/beamlog.{basename}".replace('.fits', '.txt')
        datadict[f"cube_{i}"]["beamlog"] = beamlog
        beam, nchan = getbeams(beamlog, verbose=verbose)
        datadict[f"cube_{i}"]["beam"] = beam
        datadict[f"cube_{i}"]["nchan"] = nchan
    return datadict


def commonbeamer(datadict, nchans, args, conv_mode='robust',
                 mode='natural', target_beam=None, verbose=True):
    """Find common beams

    Args:
        datadict (dict): Main data dict
        nchans (int): Number of channels
        args (args): Command line args
        conv_mode (str, optional): Convolution method
        mode (str, optional): 'total' or 'natural. Defaults to 'natural'.
        target_beam (Beam, optional): Target PSF
        verbose (bool, optional): Verbose output. Defaults to True.

    Returns:
        dict: updated datadict
    """
    ### Natural mode ###
    if mode == 'natural':
        big_beams = []
        for n in trange(
            nchans,
            desc='Constructing beams',
            disable=(not verbose)
        ):
            majors = []
            minors = []
            pas = []
            for key in datadict.keys():
                major = datadict[key]['beams'][n].major
                minor = datadict[key]['beams'][n].minor
                pa = datadict[key]['beams'][n].pa
                if datadict[key]['mask'][n]:
                    major *= np.nan
                    minor *= np.nan
                    pa *= np.nan
                majors.append(major.value)
                minors.append(minor.value)
                pas.append(pa.value)

            majors = np.array(majors)
            minors = np.array(minors)
            pas = np.array(pas)

            majors *= major.unit
            minors *= minor.unit
            pas *= pa.unit
            big_beams.append(Beams(major=majors, minor=minors, pa=pas))

        # Find common beams
        bmaj_common = []
        bmin_common = []
        bpa_common = []
        for beams in tqdm(
            big_beams,
            desc='Finding common beam per channel',
            disable=(not verbose),
            total=nchans
        ):
            if all(np.isnan(beams)):
                commonbeam = Beam(
                    major=np.nan*u.deg,
                    minor=np.nan*u.deg,
                    pa=np.nan*u.deg
                )
            else:
                try:
                    commonbeam = beams[~np.isnan(beams)].common_beam(tolerance=args.tolerance,
                                                                     nsamps=args.nsamps,
                                                                     epsilon=args.epsilon)
                except BeamError:
                    if verbose:
                        print("Couldn't find common beam with defaults")
                        print("Trying again with smaller tolerance")

                    commonbeam = beams[~np.isnan(beams)].common_beam(tolerance=args.tolerance*0.1,
                                                                     nsamps=args.nsamps,
                                                                     epsilon=args.epsilon)
                # Round up values
                commonbeam = Beam(
                    major=my_ceil(
                        commonbeam.major.to(u.arcsec).value, precision=1
                    )*u.arcsec,
                    minor=my_ceil(
                        commonbeam.minor.to(u.arcsec).value, precision=1
                    )*u.arcsec,
                    pa=round_up(commonbeam.pa.to(u.deg), decimals=2)
                )

                grid = datadict[key]["dy"]
                if conv_mode != 'robust':
                    # Get the minor axis of the convolving beams
                    minorcons = []
                    for beam in beams[~np.isnan(beams)]:
                        minorcons += [commonbeam.deconvolve(
                            beam).minor.to(u.arcsec).value]
                    minorcons = np.array(minorcons)*u.arcsec
                    samps = minorcons / grid.to(u.arcsec)
                    # Check that convolving beam will be Nyquist sampled
                    if any(samps.value < 2):
                        # Set the convolving beam to be Nyquist sampled
                        nyq_con_beam = Beam(
                            major=grid*2,
                            minor=grid*2,
                            pa=0*u.deg
                        )
                        # Find new target based on common beam * Nyquist beam
                        # Not sure if this is best - but it works
                        nyq_beam = commonbeam.convolve(nyq_con_beam)
                        nyq_beam = Beam(
                            major=my_ceil(nyq_beam.major.to(
                                u.arcsec).value, precision=1)*u.arcsec,
                            minor=my_ceil(nyq_beam.minor.to(
                                u.arcsec).value, precision=1)*u.arcsec,
                            pa=round_up(nyq_beam.pa.to(u.deg), decimals=2)
                        )
                        if verbose:
                            print(
                                'Smallest common Nyquist sampled beam is:', nyq_beam)

                        warnings.warn('COMMON BEAM WILL BE UNDERSAMPLED!')
                        warnings.warn('SETTING COMMON BEAM TO NYQUIST BEAM')
                        commonbeam = nyq_beam

            bmaj_common.append(commonbeam.major.value)
            bmin_common.append(commonbeam.minor.value)
            bpa_common.append(commonbeam.pa.value)

        bmaj_common *= commonbeam.major.unit
        bmin_common *= commonbeam.minor.unit
        bpa_common *= commonbeam.pa.unit

        # Make Beams object
        commonbeams = Beams(
            major=bmaj_common,
            minor=bmin_common,
            pa=bpa_common
        )

    elif mode == 'total':
        majors = []
        minors = []
        pas = []
        for key in datadict.keys():
            major = datadict[key]['beams'].major
            minor = datadict[key]['beams'].minor
            pa = datadict[key]['beams'].pa
            major[datadict[key]['mask']] *= np.nan
            minor[datadict[key]['mask']] *= np.nan
            pa[datadict[key]['mask']] *= np.nan
            majors.append(major.value)
            minors.append(minor.value)
            pas.append(pa.value)

        majors = np.array(majors).ravel()
        minors = np.array(minors).ravel()
        pas = np.array(pas).ravel()

        majors *= major.unit
        minors *= minor.unit
        pas *= pa.unit
        big_beams = Beams(major=majors, minor=minors, pa=pas)

        if verbose:
            print('Finding common beam across all channels')
            print('This may take some time...')

        try:
            commonbeam = big_beams[~np.isnan(big_beams)].common_beam(tolerance=args.tolerance,
                                                                     nsamps=args.nsamps,
                                                                     epsilon=args.epsilon)
        except BeamError:
            if verbose:
                print("Couldn't find common beam with defaults")
                print("Trying again with smaller tolerance")

            commonbeam = big_beams[~np.isnan(big_beams)].common_beam(tolerance=args.tolerance*0.1,
                                                                     nsamps=args.nsamps,
                                                                     epsilon=args.epsilon)
        if target_beam is not None:
            commonbeam = target_beam
        else:
            # Round up values
            commonbeam = Beam(
                major=my_ceil(
                    commonbeam.major.to(u.arcsec).value, precision=1
                )*u.arcsec,
                minor=my_ceil(
                    commonbeam.minor.to(u.arcsec).value, precision=1
                )*u.arcsec,
                pa=round_up(commonbeam.pa.to(u.deg), decimals=2)
            )
        if conv_mode != 'robust':
            # Get the minor axis of the convolving beams
            grid = datadict[key]["dy"]
            minorcons = []
            for beam in big_beams[~np.isnan(big_beams)]:
                minorcons += [commonbeam.deconvolve(
                    beam).minor.to(u.arcsec).value]
            minorcons = np.array(minorcons)*u.arcsec
            samps = minorcons / grid.to(u.arcsec)
            # Check that convolving beam will be Nyquist sampled
            if any(samps.value < 2):
                # Set the convolving beam to be Nyquist sampled
                nyq_con_beam = Beam(
                    major=grid*2,
                    minor=grid*2,
                    pa=0*u.deg
                )
                # Find new target based on common beam * Nyquist beam
                # Not sure if this is best - but it works
                nyq_beam = commonbeam.convolve(nyq_con_beam)
                nyq_beam = Beam(
                    major=my_ceil(nyq_beam.major.to(
                        u.arcsec).value, precision=1)*u.arcsec,
                    minor=my_ceil(nyq_beam.minor.to(
                        u.arcsec).value, precision=1)*u.arcsec,
                    pa=round_up(nyq_beam.pa.to(u.deg), decimals=2)
                )
                if verbose:
                    print('Smallest common Nyquist sampled beam is:', nyq_beam)
                if target_beam is not None:
                    commonbeam = target_beam
                    if target_beam < nyq_beam:
                        warnings.warn('TARGET BEAM WILL BE UNDERSAMPLED!')
                        raise Exception("CAN'T UNDERSAMPLE BEAM - EXITING")
                else:
                    warnings.warn('COMMON BEAM WILL BE UNDERSAMPLED!')
                    warnings.warn('SETTING COMMON BEAM TO NYQUIST BEAM')
                    commonbeam = nyq_beam

        # Make Beams object
        commonbeams = Beams(
            major=[commonbeam.major] * nchans * commonbeam.major.unit,
            minor=[commonbeam.minor] * nchans * commonbeam.minor.unit,
            pa=[commonbeam.pa] * nchans * commonbeam.pa.unit
        )

    if verbose:
        print('Final beams are:')
        for i, commonbeam in enumerate(commonbeams):
            print(f'Channel {i}:', commonbeam)

    for key in tqdm(
        datadict.keys(),
        desc='Getting convolution data',
        disable=(not verbose)
    ):
        # Get convolving beams
        conv_bmaj = []
        conv_bmin = []
        conv_bpa = []
        oldbeams = datadict[key]['beams']
        masks = datadict[key]['mask']
        for commonbeam, oldbeam, mask in zip(commonbeams, oldbeams, masks):
            if mask:
                convbeam = Beam(
                    major=np.nan*u.deg,
                    minor=np.nan*u.deg,
                    pa=np.nan*u.deg
                )
            else:
                convbeam = commonbeam.deconvolve(oldbeam)
            conv_bmaj.append(convbeam.major.value)
            conv_bmin.append(convbeam.minor.value)
            conv_bpa.append(convbeam.pa.to(u.deg).value)

        conv_bmaj *= convbeam.major.unit
        conv_bmin *= convbeam.minor.unit
        conv_bpa *= u.deg

        # Construct beams object
        convbeams = Beams(
            major=conv_bmaj,
            minor=conv_bmin,
            pa=conv_bpa
        )

        # Get gaussian beam factors
        facs = getfacs(datadict[key], convbeams)
        datadict[key]['facs'] = facs

        # Setup conv beamlog
        datadict[key]['convbeams'] = convbeams
        commonbeam_log = datadict[key]['beamlog'].replace('beamlog.',
                                                          f'beamlogConvolve-{mode}.')
        datadict[key]['commonbeams'] = commonbeams
        datadict[key]['commonbeamlog'] = commonbeam_log

        commonbeam_tab = Table()
        # Save target
        commonbeam_tab.add_column(np.arange(nchans), name='Channel')
        commonbeam_tab.add_column(commonbeams.major, name='Target BMAJ')
        commonbeam_tab.add_column(commonbeams.minor, name='Target BMIN')
        commonbeam_tab.add_column(commonbeams.pa, name='Target BPA')
        # Save convolving beams
        commonbeam_tab.add_column(convbeams.major, name='Convolving BMAJ')
        commonbeam_tab.add_column(convbeams.minor, name='Convolving BMIN')
        commonbeam_tab.add_column(convbeams.pa, name='Convolving BPA')
        # Save facs
        commonbeam_tab.add_column(facs, name='Convolving factor')

        # Write to log file
        units = ''
        for col in commonbeam_tab.colnames:
            unit = commonbeam_tab[col].unit
            unit = str(unit)
            units += unit + ' '
        commonbeam_tab.meta['comments'] = [units]
        ascii.write(
            commonbeam_tab,
            output=commonbeam_log,
            format='commented_header',
            overwrite=True
        )
        if verbose:
            print(f'Convolving log written to {commonbeam_log}')

    return datadict


def masking(nchans, cutoff, datadict, verbose=True):
    for key in datadict.keys():
        mask = np.array([False]*nchans)
        datadict[key]['mask'] = mask
    if cutoff is not None:
        for key in datadict.keys():
            majors = datadict[key]['beams'].major
            cutmask = majors > cutoff
            datadict[key]['mask'] += cutmask

    # Check for pipeline masking
    nullbeam = Beam(major=0*u.deg, minor=0*u.deg, pa=0*u.deg)
    for key in datadict.keys():
        nullmask = datadict[key]['beams'] == nullbeam
        datadict[key]['mask'] += nullmask
    return datadict


def initfiles(datadict, mode, suffix=None, prefix=None, verbose=True):
    """Initialise output files

    Args:
        datadict (dict): Main data dict - indexed
        mode (str): 'total' or 'natural'
        verbose (bool, optional): Verbose output. Defaults to True.

    Returns:
        datadict: Updated datadict
    """
    with fits.open(datadict["filename"], memmap=True, mode='denywrite') as hdulist:
        primary_hdu = hdulist[0]
        data = primary_hdu.data
        header = primary_hdu.header

    # Header
    commonbeams = datadict['commonbeams']
    header = commonbeams[0].attach_to_header(header)
    primary_hdu = fits.PrimaryHDU(data=data, header=header)
    if mode == 'natural':
        header['COMMENT'] = 'The PSF in each image plane varies.'
        header['COMMENT'] = 'Full beam information is stored in the second FITS extension.'
        beam_table = Table(
            data=[
                commonbeams.major.to(u.arcsec),
                commonbeams.minor.to(u.arcsec),
                commonbeams.pa.to(u.deg)
            ],
            names=[
                'BMAJ',
                'BMIN',
                'BPA'
            ]
        )
        primary_hdu = fits.PrimaryHDU(data=data.astype(np.float32), header=header)
        tab_hdu = fits.table_to_hdu(beam_table)
        new_hdulist = fits.HDUList([primary_hdu, tab_hdu])

    elif mode == 'total':
        new_hdulist = fits.HDUList([primary_hdu])

    # Set up output file
    if suffix is None:
        suffix = mode
    outname = os.path.basename(datadict["filename"])
    outname = outname.replace('.fits', f'.{suffix}.fits')
    if prefix is not None:
        outname = prefix + outname

    outdir = datadict['outdir']
    outfile = f'{outdir}/{outname}'
    if verbose:
        print(f'Initialising to {outfile}')

    new_hdulist.writeto(outfile, overwrite=True)

    return outfile


def readlogs(datadict, mode, verbose=True):
    if verbose:
        print('Reading from beamlogConvolve files')
    for key in datadict.keys():
        # Read in logs
        commonbeam_log = datadict[key]['beamlog'].replace('beamlog.',
                                                          f'beamlogConvolve-{mode}.')
        if verbose:
            print(f'Reading from {commonbeam_log}')
        try:
            commonbeam_tab = Table.read(
                commonbeam_log, format='ascii.commented_header')
        except FileNotFoundError:
            raise Exception("beamlogConvolve must be co-located with image")
        # Convert to Beams
        commonbeams = Beams(
            major=commonbeam_tab['Target BMAJ'] * u.arcsec,
            minor=commonbeam_tab['Target BMIN'] * u.arcsec,
            pa=commonbeam_tab['Target BPA'] * u.deg
        )
        convbeams = Beams(
            major=commonbeam_tab['Convolving BMAJ'] * u.arcsec,
            minor=commonbeam_tab['Convolving BMIN'] * u.arcsec,
            pa=commonbeam_tab['Convolving BPA'] * u.deg
        )
        facs = np.array(commonbeam_tab['Convolving factor'])
        # Save to datadict
        datadict[key]['facs'] = facs
        datadict[key]['convbeams'] = convbeams
        datadict[key]['commonbeams'] = commonbeams
        datadict[key]['commonbeamlog'] = commonbeam_log
    if verbose:
        print('Final beams are:')
        for i, commonbeam in enumerate(commonbeams):
            print(f'Channel {i}:', commonbeam)
    return datadict


def main(args, verbose=True):
    """main script

    Args:
        args (args): Command line args
        verbose (bool, optional): Verbose ouput. Defaults to True.

    """

    if myPE == 0:
        print(f"Total number of MPI ranks = {nPE}")
        # Parse args
        if args.dryrun:
            if verbose:
                print('Doing a dry run -- no files will be saved')

        # Check mode
        mode = args.mode
        if verbose:
            print(f"Mode is {mode}")
        if mode == 'natural' and mode == 'total':
            raise Exception("'mode' must be 'natural' or 'total'")
        if mode == 'natural':
            if verbose:
                print('Smoothing each channel to a common resolution')
        if mode == 'total':
            if verbose:
                print('Smoothing all channels to a common resolution')

        # Check cutoff
        cutoff = args.cutoff
        if args.cutoff is not None:
            cutoff = args.cutoff * u.arcsec
            if verbose:
                print('Cutoff is:', cutoff)

        # Check target
        conv_mode = args.conv_mode
        print(conv_mode)
        if not conv_mode == 'robust' and not conv_mode == 'scipy' and \
                not conv_mode == 'astropy' and not conv_mode == 'astropy_fft':
            raise Exception('Please select valid convolution method!')

        if verbose:
            print(f"Using convolution method {conv_mode}")
            if conv_mode == 'robust':
                print("This is the most robust method. And fast!")
            elif conv_mode == 'scipy':
                print('This fast, but not robust to NaNs or small PSF changes')
            else:
                print('This is slower, but robust to NaNs, but not to small PSF changes')

        bmaj = args.bmaj
        bmin = args.bmin
        bpa = args.bpa

        nonetest = [test is None for test in [bmaj, bmin, bpa]]

        if not all(nonetest) and mode != 'total':
            raise Exception("Only specify a target beam in 'total' mode")

        if all(nonetest):
            target_beam = None

        elif not all(nonetest) and any(nonetest):
            raise Exception('Please specify all target beam params!')

        elif not all(nonetest) and not any(nonetest):
            target_beam = Beam(
                bmaj * u.arcsec,
                bmin * u.arcsec,
                bpa * u.deg
            )
            if verbose:
                print('Target beam is ', target_beam)

        files = sorted(args.infile)
        if files == []:
            raise Exception('No files found!')

        outdir = args.outdir
        if outdir is not None:
            if outdir[-1] == '/':
                outdir = outdir[:-1]
            outdir = [outdir] * len(files)
        else:
            outdir = []
            for f in files:
                out = os.path.dirname(f)
                if out == '':
                    out = '.'
                outdir += [out]

        datadict = makedata(files, outdir, verbose=verbose)

        # Sanity check channel counts
        nchans = np.array([datadict[key]['nchan'] for key in datadict.keys()])
        check = all(nchans == nchans[0])

        if not check:
            raise Exception('Unequal number of spectral channels!')

        else:
            nchans = nchans[0]

        # Construct Beams objects
        for key in datadict.keys():
            beam = datadict[key]['beam']
            bmaj = np.array(beam['BMAJ'])*beam['BMAJ'].unit
            bmin = np.array(beam['BMIN'])*beam['BMIN'].unit
            bpa = np.array(beam['BPA'])*beam['BPA'].unit
            beams = Beams(
                major=bmaj,
                minor=bmin,
                pa=bpa
            )
            datadict[key]['beams'] = beams

        # Apply some masking
        datadict = masking(
            nchans,
            cutoff,
            datadict,
            verbose=verbose
        )

        if not args.uselogs:
            datadict = commonbeamer(
                datadict,
                nchans,
                args,
                conv_mode=conv_mode,
                target_beam=target_beam,
                mode=mode,
                verbose=verbose
            )
        else:
            datadict = readlogs(
                datadict,
                mode=mode,
                verbose=verbose
            )

    else:
        if not args.dryrun:
            files = None
            datadict = None
            nchans = None

    if mpiSwitch:
        comm.Barrier()

    # Init the files in parallel
    if not args.dryrun:
        if myPE == 0 and verbose:
            print('Initialising output files')
        if mpiSwitch:
            files = comm.bcast(files, root=0)
            datadict = comm.bcast(datadict, root=0)
            nchans = comm.bcast(nchans, root=0)

        conv_mode = args.conv_mode
        inputs = list(datadict.keys())
        dims = len(inputs)

        if nPE > dims:
            my_start = myPE
            my_end = myPE

        else:
            count = dims // nPE
            rem = dims % nPE
            if myPE < rem:
                # The first 'remainder' ranks get 'count + 1' tasks each
                my_start = myPE * (count + 1)
                my_end = my_start + count

            else:
                # The remaining 'size - remainder' ranks get 'count' task each
                my_start = myPE * count + rem
                my_end = my_start + (count - 1)

        if verbose:
            if myPE == 0:
                print(
                    f"There are {dims} files to init")
            print(f"My start is {my_start}", f"My end is {my_end}")

        # Init output files and retrieve file names
        outfile_dict = {}
        for inp in inputs[my_start:my_end+1]:
            outfile = initfiles(
                datadict[inp],
                args.mode,
                suffix=args.suffix,
                prefix=args.prefix,
                verbose=verbose
            )
            outfile_dict.update(
                {
                    inp: outfile
                }
            )

        if mpiSwitch:
            # Send to master proc
            outlist = comm.gather(outfile_dict, root=0)

        if mpiSwitch:
            comm.Barrier()

        # Now do the convolution in parallel
        if myPE == 0:

            # Conver list to dict and save to main dict
            outlist_dict = {}
            for d in outlist:
                outlist_dict.update(d)
            # Also make inputs list
            inputs = []
            for key in datadict.keys():
                datadict[key]['outfile'] = outlist_dict[key]
                for chan in range(nchans):
                    inputs.append((key, chan))

        else:
            datadict = None
            inputs = None
        if mpiSwitch:
            comm.Barrier()
        if mpiSwitch:
            inputs = comm.bcast(inputs, root=0)
            datadict = comm.bcast(datadict, root=0)

        dims = len(files) * nchans
        assert len(inputs) == dims
        count = dims // nPE
        rem = dims % nPE
        if myPE < rem:
            # The first 'remainder' ranks get 'count + 1' tasks each
            my_start = myPE * (count + 1)
            my_end = my_start + count

        else:
            # The remaining 'size - remainder' ranks get 'count' task each
            my_start = myPE * count + rem
            my_end = my_start + (count - 1)
        if verbose:
            if myPE == 0:
                print(
                    f"There are {nchans} channels, across {len(files)} files")
            print(f"My start is {my_start}", f"My end is {my_end}")

        for inp in inputs[my_start:my_end+1]:
            key, chan = inp
            newim = worker(chan, datadict[key], conv_mode=conv_mode)
            outfile = datadict[key]['outfile']
            with fits.open(outfile, mode='update', memmap=True) as outfh:
                outfh[0].data[chan, 0, :, :] = newim.astype(np.float32) # make sure data is 32-bit
                outfh.flush()
            if verbose:
                print(f"{outfile}  - channel {chan} - Done")

    if verbose:
        print('Done!')


def cli():
    """Command-line interface
    """
    import argparse

    # Help string to be shown using the -h option
    descStr = """
    Smooth a field of 3D cubes to a common resolution.

    Names of output files are 'infile'.sm.fits

    """

    # Parse the command line options
    parser = argparse.ArgumentParser(description=descStr,
                                     formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument(
        'infile',
        metavar='infile',
        type=str,
        help="""Input FITS image(s) to smooth (can be a wildcard) 
        - beam info must be in co-located beamlog files.
        """,
        nargs='+')

    parser.add_argument(
        "--uselogs",
        dest="uselogs",
        action="store_true",
        help="Get convolving information from previous run [False]."
    )

    parser.add_argument(
        '--mode',
        dest='mode',
        type=str,
        default='natural',
        help="""Common resolution mode [natural]. 
        natural  -- allow frequency variation.
        total -- smooth all plans to a common resolution.
        """
    )

    parser.add_argument(
        "--conv_mode",
        dest="conv_mode",
        type=str,
        default='robust',
        help="""Which method to use for convolution [robust].
        'robust' computes the analytic FT of the convolving Gaussian.
        Can also be 'scipy', 'astropy', or 'astropy_fft'.
        Note these other methods cannot cope well with small convolving beams.
        """
    )

    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        help="verbose output [False]."
    )

    parser.add_argument(
        "-d",
        "--dryrun",
        dest="dryrun",
        action="store_true",
        help="Compute common beam and stop [False]."
    )

    parser.add_argument(
        '-p',
        '--prefix',
        dest='prefix',
        type=str,
        default=None,
        help='Add prefix to output filenames.')

    parser.add_argument(
        '-s',
        '--suffix',
        dest='suffix',
        type=str,
        default=None,
        help='Add suffix to output filenames [...{mode}.fits].')

    parser.add_argument(
        '-o',
        '--outdir',
        dest='outdir',
        type=str,
        default=None,
        help='Output directory of smoothed FITS image(s) [None - same as input].'
    )

    parser.add_argument(
        "--bmaj",
        dest="bmaj",
        type=float,
        default=None,
        help="BMAJ to convolve to [max BMAJ from given image(s)]."
    )

    parser.add_argument(
        "--bmin",
        dest="bmin",
        type=float,
        default=None,
        help="BMIN to convolve to [max BMAJ from given image(s)]."
    )

    parser.add_argument(
        "--bpa",
        dest="bpa",
        type=float,
        default=None,
        help="BPA to convolve to [0]."
    )

    parser.add_argument(
        '-c',
        '--cutoff',
        dest='cutoff',
        type=float,
        default=None,
        help='Cutoff BMAJ value (arcsec) -- Blank channels with BMAJ larger than this [None -- no limit]'
    )

    parser.add_argument(
        "-t",
        "--tolerance",
        dest="tolerance",
        type=float,
        default=0.0001,
        help="tolerance for radio_beam.commonbeam."
    )

    parser.add_argument(
        "-e",
        "--epsilon",
        dest="epsilon",
        type=float,
        default=0.0005,
        help="epsilon for radio_beam.commonbeam."
    )

    parser.add_argument(
        "-n",
        "--nsamps",
        dest="nsamps",
        type=int,
        default=200,
        help="nsamps for radio_beam.commonbeam."
    )

    args = parser.parse_args()

    verbose = args.verbose

    main(args, verbose=verbose)


if __name__ == "__main__":
    cli()
