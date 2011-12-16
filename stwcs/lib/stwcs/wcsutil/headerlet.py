"""
This module implements headerlets.

A headerlet serves as a mechanism for encapsulating WCS information
which can be used to update the WCS solution of an image. The idea
came up first from the desire for passing improved astrometric
solutions for HST data and provide those solutions in a manner
that would not require getting entirely new images from the archive
when only the WCS information has been updated.

"""

from __future__ import division
import functools
import logging
import os
import textwrap
import copy
import time

import numpy as np
import pyfits

import altwcs
import wcscorr
from hstwcs import HSTWCS
from mappings import basic_wcs
from stwcs.updatewcs import utils

from stsci.tools.fileutil import countExtn
from stsci.tools import fileutil as fu
from stsci.tools import parseinput

#### Logging support functions
class FuncNameLoggingFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None):
        if '%(funcName)s' not in fmt:
            fmt = '%(funcName)s' + fmt
        logging.Formatter.__init__(self, fmt=fmt, datefmt=datefmt)

    def format(self, record):
        record = copy.copy(record)
        if hasattr(record, 'funcName') and record.funcName == 'init_logging':
            record.funcName = ''
        else:
            record.funcName += ' '
        return logging.Formatter.format(self, record)


logger = logging.getLogger(__name__)
formatter = FuncNameLoggingFormatter("%(levelname)s: %(message)s")
ch = logging.StreamHandler()
ch.setFormatter(formatter)
ch.setLevel(logging.CRITICAL)
logger.addHandler(ch)
logger.setLevel(logging.DEBUG)

FITS_STD_KW = ['XTENSION', 'BITPIX', 'NAXIS', 'PCOUNT',
             'GCOUNT', 'EXTNAME', 'EXTVER', 'ORIGIN',
             'INHERIT', 'DATE', 'IRAF-TLM']

DEFAULT_SUMMARY_COLS = ['HDRNAME', 'WCSNAME', 'DISTNAME', 'AUTHOR', 'DATE',
                        'SIPNAME', 'NPOLFILE', 'D2IMFILE', 'DESCRIP']
COLUMN_DICT = {'vals': [], 'width': []}
COLUMN_FMT = '{:<{width}}'


def init_logging(funcname=None, level=100, mode='w', **kwargs):
    """ Initialize logging for a function

    Parameters
    ----------
    funcname: string
        Name of function which will be recorded in log
    level: int, or bool, or string
           int or string : Logging level
           bool: False - switch off logging
        Text logging level for the message ("DEBUG", "INFO",
                        "WARNING", "ERROR", "CRITICAL")
    mode: 'w' or 'a'
           attach to logfile ('a' or start a new logfile ('w')
    """
    for hndl in logger.handlers:
        if isinstance(hndl, logging.FileHandler):
            has_file_handler = True
        else:
            has_file_handler = False
    if level:
        if not has_file_handler:
            logname = 'headerlet.log'
            fh = logging.FileHandler(logname, mode=mode)
            fh.setFormatter(formatter)
            fh.setLevel(logging.DEBUG)
            logger.addHandler(fh)
        logger.info("%s: Starting %s with arguments:\n\t %s" %
                    (time.asctime(), funcname, kwargs))


def with_logging(func):
    @functools.wraps(func)
    def wrapped(*args, **kw):
        level = kw.get('logging', 100)
        mode = kw.get('logmode', 'w')
        func_args = kw.copy()
        for argname, arg in zip(func.func_code.co_varnames, args):
            func_args[argname] = arg
        init_logging(func.__name__, level, mode, **func_args)
        return func(*args, **kw)
    return wrapped

#### Utility functions
def is_par_blank(par):
    return par in ['', ' ', 'INDEF', "None", None]

def parse_filename(fname, mode='readonly'):
    """
    Interprets the input as either a filename of a file that needs to be opened
    or a PyFITS object.

    Parameters
    ----------
    fname: string, pyfits.HDUList
        Input pointing to a file or PyFITS object. An input filename (str) will
        be expanded as necessary to interpret any environmental variables
        included in the filename.

    mode: string
        Specifies what PyFITS mode to use when opening the file, if it needs
        to open the file at all [Default: 'readonly']

    Returns
    -------
    fobj: pyfits.HDUList
        PyFITS handle for input file

    fname: string
        Name of input file

    close_fobj: bool
        Flag specifying whether or not fobj needs to be closed since it was
        opened by this function. This allows a program to know whether they
        need to worry about closing the PyFITS object as opposed to letting
        the higher level interface close the object.
    """

    close_fobj = False
    if not isinstance(fname, list):
        if isinstance(fname, basestring):
            fname = fu.osfn(fname)
        fobj = pyfits.open(fname, mode=mode)
        close_fobj = True
    else:
        fobj = fname
        if hasattr(fobj, 'filename'):
            fname = fobj.filename()
        else:
            fname = ''
    return fobj, fname, close_fobj

def get_headerlet_kw_names(fobj, kw='HDRNAME'):
    """
    Returns a list of specified keywords from all HeaderletHDU
    extensions in a science file.

    Parameters
    ----------
    fobj: string, pyfits.HDUList
    kw: str
        Name of keyword to be read and reported
    """

    fobj, fname, open_fobj = parse_filename(fobj)

    hdrnames = []
    for ext in fobj:
        if isinstance(ext, pyfits.hdu.base.NonstandardExtHDU):
            hdrnames.append(ext.header[kw])

    if open_fobj:
        fobj.close()

    return hdrnames

def get_header_kw_vals(hdr, kwname, kwval, default=0):
    if kwval is None:
        if kwname in hdr:
            kwval = hdr[kwname]
        else:
            kwval = default
    return kwval


@with_logging
def find_headerlet_HDUs(fobj, hdrext=None, hdrname=None, distname=None,
                        strict=True, logging=False, logmode='w'):
    """
    Returns all HeaderletHDU extensions in a science file that matches
    the inputs specified by the user.  If no hdrext, hdrname or distname are
    specified, this function will return a list of all HeaderletHDU objects.

    Parameters
    ----------
    fobj: string, pyfits.HDUList
        Name of FITS file or open pyfits object (pyfits.HDUList instance)
    hdrext: int, tuple or None
        index number(EXTVER) or extension tuple of HeaderletHDU to be returned
    hdrname: string
        value of HDRNAME for HeaderletHDU to be returned
    distname: string
        value of DISTNAME for HeaderletHDUs to be returned
    strict: bool [Default: True]
        Specifies whether or not at least one parameter needs to be provided
        If False, all extension indices returned if hdrext, hdrname and distname
        are all None. If True and hdrext, hdrname, and distname are all None,
        raise an Exception requiring one to be specified.
    logging: boolean
             enable logging to a file called headerlet.log
    logmode: 'w' or 'a'
             log file open mode

    Returns
    -------
    hdrlets: list
        A list of all matching HeaderletHDU extension indices (could be just one)

    """

    get_all = False
    if hdrext is None and hdrname is None and distname is None:
        if not strict:
            get_all = True
        else:
            mess = """\n
            =====================================================
            No valid Headerlet extension specified.
            Either "hdrname", "hdrext", or "distname" needs to be specified.
            =====================================================
            """
            logger.critical(mess)
            raise ValueError

    fobj, fname, open_fobj = parse_filename(fobj)

    hdrlets = []
    if hdrext is not None and isinstance(hdrext, int):
        if hdrext in range(len(fobj)): # insure specified hdrext is in fobj
            if isinstance(fobj[hdrext], pyfits.hdu.base.NonstandardExtHDU) and \
                fobj[hdrext].header['EXTNAME'] == 'HDRLET':
                hdrlets.append(hdrext)
    else:
        for ext in fobj:
            if isinstance(ext, pyfits.hdu.base.NonstandardExtHDU):
                if get_all:
                    hdrlets.append(fobj.index(ext))
                else:
                    if hdrext is not None:
                        if isinstance(hdrext, tuple):
                            hdrextname = hdrext[0]
                            hdrextnum = hdrext[1]
                        else:
                            hdrextname = 'HDRLET'
                            hdrextnum = hdrext
                    hdrext_match = ((hdrext is not None) and
                                    (hdrextnum == ext.header['EXTVER']) and
                                    (hdrextname == ext.header['EXTNAME']))
                    hdrname_match = ((hdrname is not None) and
                                        (hdrname == ext.header['HDRNAME']))
                    distname_match = ((distname is not None) and
                                        (distname == ext.header['DISTNAME']))
                    if hdrext_match or hdrname_match or distname_match:
                        hdrlets.append(fobj.index(ext))

    if open_fobj:
        fobj.close()

    if len(hdrlets) == 0:
        if hdrname:
            kwerr = 'hdrname'
            kwval = hdrname
        elif hdrext:
            kwerr = 'hdrext'
            kwval = hdrext
        else:
            kwerr = 'distname'
            kwval = distname
        message = """\n
        =====================================================
        No valid Headerlet extension found!'
        "%s" = %s not found in %s.' % (kwerr, kwval, fname)
        =====================================================
        """
        logger.critical(message)
        raise ValueError

    return hdrlets

def verify_hdrname_is_unique(fobj, hdrname):
    """
    Verifies that no other HeaderletHDU extension has the specified hdrname.

    Parameters
    ----------
    fobj: string, pyfits.HDUList
        Name of FITS file or open pyfits object (pyfits.HDUList instance)
    hdrname: string
        value of HDRNAME for HeaderletHDU to be compared as unique

    Returns
    -------
    unique: bool
        If True, no other HeaderletHDU has the specified HDRNAME value
    """
    hdrnames_list = get_headerlet_kw_names(fobj)
    unique = not(hdrname in hdrnames_list)

    return unique


@with_logging
def is_wcs_identical(scifile, file2, scikey=" ", file2key=" ", logging=False):
    """
    Compares the WCS solution of 2 files.

    Parameters
    ----------
    scifile: string
             name of file1 (usually science file)
             IRAF style extension syntax is accepted as well
             for example scifile[1] or scifile[sci,1]
    file2:   string
             name of second file (for example headerlet)
    scikey:  string
             alternate WCS key in scifile
    file2key: string
             alternate WCS key in file2
    logging: boolean
             True: enable file logging

    Notes
    -----
    These can be 2 science observations or 2 headerlets
    or a science observation and a headerlet. The two files
    have the same WCS solution if the following are the same:

    - rootname/destim
    - primary WCS
    - SIP coefficients
    - NPOL distortion
    - D2IM correction
    - Velocity aberation

    """

    fname, extname = fu.parseFilename(scifile)
    scifile = fname
    if extname is not None:
        sciext = fu.parseExtn(extname)
    else:
        sciext = None
    fname, extname = fu.parseFilename(file2)
    file2 = fname
    if extname is not None:
        fext = fu.parseExtn(extname)
    else:
        fext = None
    result = True
    if sciext is None and fext is None:
        numsci1 = max(countExtn(scifile), countExtn(scifile, 'SIPWCS'))
        numsci2 = max(countExtn(file2), countExtn(file2, 'SIPWCS'))

        if numsci1 == 0 or numsci2 == 0 or numsci1 != numsci2:
            logger.info("Number of SCI and SIPWCS extensions do not match.")
            result = False
    else:
        numsci1 = None
        numsci2 = None

    if get_rootname(scifile) != get_rootname(file2):
        logger.info('Rootnames do not match.')
        result = False
    try:
        extname1 = pyfits.getval(scifile, 'EXTNAME', ext=('SCI', 1))
    except KeyError:
        extname1 = 'SIPWCS'
    try:
        extname2 = pyfits.getval(file2, 'EXTNAME', ext=('SCI', 1))
    except KeyError:
        extname2 = 'SIPWCS'

    if numsci1 and numsci2:
        sciextlist = [(extname1, i) for i in range(1, numsci1+1)]
        fextlist = [(extname2, i) for i in range(1, numsci2+1)]
    else:
        sciextlist = [sciext]
        fextlist = [fext]

    for i, j in zip(sciextlist, fextlist):
        w1 = HSTWCS(scifile, ext=i, wcskey=scikey)
        w2 = HSTWCS(file2, ext=j, wcskey=file2key)
        if not np.allclose(w1.wcs.crval, w2.wcs.crval, rtol=1e-7) or \
        not np.allclose(w1.wcs.crpix, w2.wcs.crpix, rtol=1e-7)  or \
        not np.allclose(w1.wcs.cd, w2.wcs.cd, rtol=1e-7) or \
        not (np.array(w1.wcs.ctype) == np.array(w2.wcs.ctype)).all():
            logger.info('Primary WCSs do not match')
            result = False
        if w1.sip or w2.sip:
            if (w2.sip and not w1.sip) or (w1.sip and not w2.sip) or \
               not np.allclose(w1.sip.a, w2.sip.a, rtol=1e-7) or \
               not np.allclose(w1.sip.b, w2.sip.b, rtol=1e-7):
                logger.info('SIP coefficients do not match')
                result = False
        if w1.cpdis1 or w2.cpdis1:
            if w1.cpdis1 and not w2.cpdis1 or \
                w2.cpdis1 and not w1.cpdis1 or \
                not np.allclose(w1.cpdis1.data, w2.cpdis1.data):
                logger.info('NPOL distortions do not match')
                result = False
        if w1.cpdis2 or w2.cpdis2:
            if w1.cpdis2 and not w2.cpdis2 or \
                w2.cpdis2 and not w1.cpdis2 or \
                not np.allclose(w1.cpdis2.data, w2.cpdis2.data):
                logger.info('NPOL distortions do not match')
                result = False
        if w1.det2im1 or w2.det2im1:
            if w1.det2im1 and not w2.det2im1 or \
                w2.det2im1 and not w1.det2im1 or\
                not np.allclose(w1.det2im1.data, w2.det2im1.data):
                logger.info('Det2Im corrections do not match')
                result =  False
        if w1.det2im2 or w2.det2im2:
            if w1.det2im2 and not w2.det2im2 or \
                w2.det2im2 and not w1.det2im2 or\
                not np.allclose(w1.det2im2.data, w2.det2im2.data):
                logger.info('Det2Im corrections do not match')
                result = False
        if w1.vafactor != w2.vafactor:
            logger.info('VA factors do not match')
            result = False

    return result

def update_ref_files(source, dest):
    """
    Update the reference files name in the primary header of 'dest'
    using values from 'source'

    Parameters
    ----------
    source: pyfits.Header
    dest:   pyfits.Header
    """
    logger.info("Updating reference files")
    phdukw = {'IDCTAB': True,
            'NPOLFILE': True,
            'D2IMFILE': True}

    if 'HISTORY' in dest:
        wind = dest.ascard.index_of('HISTORY')
    else:
        wind = len(dest)

    for key in phdukw.keys():
        try:
            srckey = source.ascard[key]
            dest.update(key, srckey.value, after=wind, comment=srckey.comment)
        except KeyError:
            # TODO: I don't understand what the point of this is.  Is it meant
            # for logging purposes?  Right now it isn't used.
            phdukw[key] = False
    return phdukw

def get_rootname(fname):
    """
    returns the value of ROOTNAME or DESTIM
    """

    try:
        rootname = pyfits.getval(fname, 'ROOTNAME')
    except KeyError:
        rootname = pyfits.getval(fname, 'DESTIM')
    return rootname

def print_summary(summary_cols, summary_dict, pad=2, maxwidth=None, idcol=None,
                    output=None, clobber=True, quiet=False ):
    """
    Print out summary dictionary to STDOUT, and possibly an output file

    """
    nrows = None
    if idcol:
        nrows = len(idcol['vals'])

    # Find max width of each column
    column_widths = {}
    for kw in summary_dict:
        colwidth = np.array(summary_dict[kw]['width']).max()
        if maxwidth:
            colwidth = min(colwidth, maxwidth)
        column_widths[kw] = colwidth + pad
        if nrows is None:
            nrows = len(summary_dict[kw]['vals'])

    # print rows now
    outstr = ''
    # Start with column names
    if idcol:
        outstr += COLUMN_FMT.format(idcol['name'], width=idcol['width'] + pad)
    for kw in summary_cols:
        outstr += COLUMN_FMT.format(kw, width=column_widths[kw])
    outstr += '\n'
    # Now, add a row for each headerlet
    for row in range(nrows):
        if idcol:
            outstr += COLUMN_FMT.format(idcol['vals'][row],
                                        width=idcol['width']+pad)
        for kw in summary_cols:
            val = summary_dict[kw]['vals'][row][:(column_widths[kw]-pad)]
            outstr += COLUMN_FMT.format(val, width=column_widths[kw])
        outstr += '\n'
    if not quiet:
        print outstr

    # If specified, write info to separate text file
    write_file = False
    if output:
        output = fu.osfn(output) # Expand any environment variables in filename
        write_file = True
        if os.path.exists(output):
            if clobber:
                os.remove(output)
            else:
                print 'WARNING: Not writing results to file!'
                print '         Output text file ', output, ' already exists.'
                print '         Set "clobber" to True or move file before trying again.'
                write_file = False
        if write_file:
            fout = open(output, mode='w')
            fout.write(outstr)
            fout.close()

#### Private utility functions
def _create_primary_HDU(destim, hdrname, distname, wcsname,
                            sipname, npolfile, d2imfile,
                            rms_ra,rms_dec,nmatch,catalog,
                            upwcsver, pywcsver,
                            author, descrip, history):
    # convert input values into valid FITS kw values
    if author is None:
        author = ''
    if descrip is None:
        descrip = ''
    if history is None:
        history = ''

    # build Primary HDU
    phdu = pyfits.PrimaryHDU()
    phdu.header.update('DESTIM', destim,
                       comment='Destination observation root name')
    phdu.header.update('HDRNAME', hdrname, comment='Headerlet name')
    fmt = "%Y-%m-%dT%H:%M:%S"
    phdu.header.update('DATE', time.strftime(fmt),
                       comment='Date FITS file was generated')
    phdu.header.update('WCSNAME', wcsname, comment='WCS name')
    phdu.header.update('DISTNAME', distname, comment='Distortion model name')
    phdu.header.update('SIPNAME', sipname, comment='origin of SIP polynomial distortion model')
    phdu.header.update('NPOLFILE', npolfile, comment='origin of non-polynmial distortion model')
    phdu.header.update('D2IMFILE', d2imfile, comment='origin of detector to image correction')
    phdu.header.update('AUTHOR', author, comment='headerlet created by this user')
    phdu.header.update('DESCRIP', descrip, comment='Short description of headerlet solution')
    phdu.header.update('RMS_RA', rms_ra, comment='RMS in RA at ref pix of headerlet solution')
    phdu.header.update('RMS_DEC', rms_dec, comment='RMS in Dec at ref pix of headerlet solution')
    phdu.header.update('NMATCH', nmatch, comment='Number of sources used for headerlet solution')
    phdu.header.update('CATALOG', catalog, comment='Astrometric catalog used for headerlet solution')
    phdu.header.update('UPWCSVER', upwcsver.value, comment=upwcsver.comment)
    phdu.header.update('PYWCSVER', pywcsver.value, comment=pywcsver.comment)

    # clean up history string in order to remove whitespace characters that
    # would cause problems with FITS
    if isinstance(history, list):
        history_str = ''
        for line in history:
            history_str += line
    else:
        history_str = history
    history_lines = textwrap.wrap(history_str, width=70)
    for hline in history_lines:
        phdu.header.add_history(hline)

    return phdu


#### Public Interface functions
@with_logging
def extract_headerlet(filename, output, extnum=None, hdrname=None,
                      clobber=False, logging=True):
    """
    Finds a headerlet extension in a science file and writes it out as
    a headerlet FITS file.

    If both hdrname and extnum are given they should match, if not
    raise an Exception

    Parameters
    ----------
    filename: string or HDUList or Python list
        This specifies the name(s) of science file(s) from which headerlets
        will be extracted.

        String input formats supported include use of wild-cards, IRAF-style
        '@'-files (given as '@<filename>') and comma-separated list of names.
        An input filename (str) will be expanded as necessary to interpret
        any environmental variables included in the filename.
        If a list of filenames has been specified, it will extract a
        headerlet from the same extnum from all filenames.
    output: string
           Filename or just rootname of output headerlet FITS file
           If string does not contain '.fits', it will create a filename with
           '_hlet.fits' suffix
    extnum: int
           Extension number which contains the headerlet to be written out
    hdrname: string
           Unique name for headerlet, stored as the HDRNAME keyword
           It stops if a value is not provided and no extnum has been specified
    clobber: bool
        If output file already exists, this parameter specifies whether or not
        to overwrite that file [Default: False]
    logging: boolean
             enable logging to a file

    """

    if isinstance(filename, pyfits.HDUList):
        filename = [filename]
    else:
        filename, oname = parseinput.parseinput(filename)

    for f in filename:
        fobj, fname, close_fobj = parse_filename(f)
        frootname = fu.buildNewRootname(fname)
        if hdrname in ['', ' ', None, 'INDEF'] and extnum is None:
            if close_fobj:
                fobj.close()
                logger.critical("Expected a valid extnum or hdrname parameter")
                raise ValueError
        if hdrname is not None:
            extn_from_hdrname = find_headerlet_HDUs(fobj, hdrname=hdrname)[0]
            if extn_from_hdrname != extnum:
                logger.critical("hdrname and extnmu should refer to the same FITS extension")
                raise ValueError
            else:
                hdrhdu = fobj[extn_from_hdrname]
        else:
            hdrhdu = fobj[extnum]

        if not isinstance(hdrhdu, HeaderletHDU):
            logger.critical("Specified extension is not a headerlet")
            raise ValueError

        hdrlet = hdrhdu.headerlet

        if output is None:
            output = frootname

        if '.fits' in output:
            outname = output
        else:
            outname = '%s_hlet.fits' % output

        hdrlet.tofile(outname, clobber=clobber)

        if close_fobj:
            fobj.close()


@with_logging
def write_headerlet(filename, hdrname, output=None, sciext='SCI',
                        wcsname=None, wcskey=None, destim=None,
                        sipname=None, npolfile=None, d2imfile=None,
                        author=None, descrip=None, history=None,
                        rms_ra=None, rms_dec=None, nmatch=None, catalog=None,
                        attach=True, clobber=False, logging=False):

    """
    Save a WCS as a headerlet FITS file.

    This function will create a headerlet, write out the headerlet to a
    separate headerlet file, then, optionally, attach it as an extension
    to the science image (if it has not already been archived)

    Either wcsname or wcskey must be provided; if both are given, they must
    match a valid WCS.

    Updates wcscorr if necessary.

    Parameters
    ----------
    filename: string or HDUList or Python list
        This specifies the name(s) of science file(s) from which headerlets
        will be created and written out.
        String input formats supported include use of wild-cards, IRAF-style
        '@'-files (given as '@<filename>') and comma-separated list of names.
        An input filename (str) will be expanded as necessary to interpret
        any environmental variables included in the filename.
    hdrname: string
        Unique name for this headerlet, stored as HDRNAME keyword
    output: string or None
        Filename or just rootname of output headerlet FITS file
        If string does not contain '.fits', it will create a filename
        starting with the science filename and ending with '_hlet.fits'.
        If None, a default filename based on the input filename will be
        generated for the headerlet FITS filename
    sciext: string
        name (EXTNAME) of extension that contains WCS to be saved
    wcsname: string
        name of WCS to be archived, if " ": stop
    wcskey: one of A...Z or " " or "PRIMARY"
        if " " or "PRIMARY" - archive the primary WCS
    destim: string
        DESTIM keyword
        if  NOne, use ROOTNAME or science file name
    sipname: string or None (default)
         Name of unique file where the polynomial distortion coefficients were
         read from. If None, the behavior is:
         The code looks for a keyword 'SIPNAME' in the science header
         If not found, for HST it defaults to 'IDCTAB'
         If there is no SIP model the value is 'NOMODEL'
         If there is a SIP model but no SIPNAME, it is set to 'UNKNOWN'
    npolfile: string or None (default)
         Name of a unique file where the non-polynomial distortion was stored.
         If None:
         The code looks for 'NPOLFILE' in science header.
         If 'NPOLFILE' was not found and there is no npol model, it is set to 'NOMODEL'
         If npol model exists, it is set to 'UNKNOWN'
    d2imfile: string
         Name of a unique file where the detector to image correction was
         stored. If None:
         The code looks for 'D2IMFILE' in the science header.
         If 'D2IMFILE' is not found and there is no d2im correction,
         it is set to 'NOMODEL'
         If d2im correction exists, but 'D2IMFILE' is missing from science
         header, it is set to 'UNKNOWN'
    author: string
        Name of user who created the headerlet, added as 'AUTHOR' keyword
        to headerlet PRIMARY header
    descrip: string
        Short description of the solution provided by the headerlet
        This description will be added as the single 'DESCRIP' keyword
        to the headerlet PRIMARY header
    history: filename, string or list of strings
        Long (possibly multi-line) description of the solution provided
        by the headerlet. These comments will be added as 'HISTORY' cards
        to the headerlet PRIMARY header
        If filename is specified, it will format and attach all text from
        that file as the history.
    attach: bool
        Specify whether or not to attach this headerlet as a new extension
        It will verify that no other headerlet extension has been created with
        the same 'hdrname' value.
    clobber: bool
        If output file already exists, this parameter specifies whether or not
        to overwrite that file [Default: False]
    logging: boolean
         enable file logging
    """

    if isinstance(filename, pyfits.HDUList):
        filename = [filename]
    else:
        filename, oname = parseinput.parseinput(filename)

    for f in filename:
        if isinstance(f, str):
            fname = f
        else:
            fname = f.filename()

        if wcsname in [None, ' ', '', 'INDEF'] and wcskey is None:
            message = """\n
            No valid WCS found found in %s.
            A valid value for either "wcsname" or "wcskey" 
            needs to be specified.
            """ % fname
            logger.critical(message)
            raise ValueError

        # Translate 'wcskey' value for PRIMARY WCS to valid altwcs value of ' '
        if wcskey == 'PRIMARY':
            wcskey = ' '

        if attach:
            umode = 'update'
        else:
            umode = 'readonly'

        fobj, fname, close_fobj = parse_filename(f, mode=umode)
        wnames = altwcs.wcsnames(fobj,ext=('sci',1))

        # Insure that WCSCORR table has been created with all original
        # WCS's recorded prior to adding the headerlet WCS
        wcscorr.init_wcscorr(fobj)

        if wcsname is None:
            scihdr = fobj[sciext, 1].header
            wname = scihdr['wcsname'+wcskey]
        else:
            wname = wcsname
        if hdrname in [None, ' ', '']:
            hdrname = wcsname

        logger.critical('Creating the headerlet from image %s' % fname)
        hdrletobj = create_headerlet(fobj, sciext=sciext,
                                    wcsname=wname, wcskey=wcskey,
                                    hdrname=hdrname,
                                    sipname=sipname, npolfile=npolfile,
                                    d2imfile=d2imfile, author=author,
                                    descrip=descrip, history=history,
                                    rms_ra=rms_ra, rms_dec=rms_dec,
                                    nmatch=nmatch, catalog=catalog,
                                    logging=False)
        
        if attach:
            # Check to see whether or not a HeaderletHDU with
            #this hdrname already exists
            hdrnames = get_headerlet_kw_names(fobj)
            if hdrname not in hdrnames:
                hdrlet_hdu = HeaderletHDU.fromheaderlet(hdrletobj)

                if destim is not None:
                    hdrlet_hdu[0].header['destim'] = destim

                fobj.append(hdrlet_hdu)

                # Update the WCSCORR table with new rows from the headerlet's WCSs
                wcscorr.update_wcscorr(fobj, source=hdrletobj,
                                       extname='SIPWCS', wcs_id=wname)

                fobj.flush()
            else:
                message = """
                Headerlet with hdrname %s already archived for WCS %s.
                No new headerlet appended to %s.
                """ % (hdrname, wname, fname)
                logger.critical(message)

        if close_fobj:
            fobj.close()

        frootname = fu.buildNewRootname(fname)
        if output is None:
            # Generate default filename for headerlet FITS file
            outname = '%s_hlet.fits' % (frootname)
        else:
            outname = output
        if '.fits' not in outname:
            outname = '%s_%s_hlet.fits' % (frootname, outname)

        # If user specifies an output filename for headerlet, write it out

        hdrletobj.tofile(outname, clobber=clobber)
        logger.critical( 'Created Headerlet file %s ' % outname)


@with_logging
def create_headerlet(filename, sciext='SCI', hdrname=None, destim=None,
                     wcskey=" ", wcsname=None,
                     sipname=None, npolfile=None, d2imfile=None,
                     author=None, descrip=None, history=None,
                     rms_ra=None, rms_dec = None, nmatch=None, catalog=None,
                     logging=False, logmode='w'):
    """
    Create a headerlet from a WCS in a science file
    If both wcskey and wcsname are given they should match, if not
    raise an Exception

    Parameters
    ----------
    filename: string or HDUList
           Either a filename or PyFITS HDUList object for the input science file
            An input filename (str) will be expanded as necessary to interpret
            any environmental variables included in the filename.
    sciext: string or python list (default: 'SCI')
           Extension in which the science data is. The headerlet will be created
           from these extensions.
           If string - a valid EXTNAME is expected
           If int - specifies an extension with a valid WCS, such as 0 for a
                simple FITS file
           If list - a list of FITS extension numbers or strings representing
           extension tuples, e.g. ('SCI, 1') is expected.
    hdrname: string
           value of HDRNAME keyword
           Takes the value from the HDRNAME<wcskey> keyword, if not available from WCSNAME<wcskey>
           It stops if neither is found in the science file and a value is not provided
    destim: string or None
            name of file this headerlet can be applied to
            if None, use ROOTNAME keyword
    wcskey: char (A...Z) or " " or "PRIMARY" or None
            a char representing an alternate WCS to be used for the headerlet
            if " ", use the primary (default)
            if None use wcsname
    wcsname: string or None
            if wcskey is None use wcsname specified here to choose an alternate WCS for the headerlet
    sipname: string or None (default)
             Name of unique file where the polynomial distortion coefficients were
             read from. If None, the behavior is:
             The code looks for a keyword 'SIPNAME' in the science header
             If not found, for HST it defaults to 'IDCTAB'
             If there is no SIP model the value is 'NOMODEL'
             If there is a SIP model but no SIPNAME, it is set to 'UNKNOWN'
    npolfile: string or None (default)
             Name of a unique file where the non-polynomial distortion was stored.
             If None:
             The code looks for 'NPOLFILE' in science header.
             If 'NPOLFILE' was not found and there is no npol model, it is set to 'NOMODEL'
             If npol model exists, it is set to 'UNKNOWN'
    d2imfile: string
             Name of a unique file where the detector to image correction was
             stored. If None:
             The code looks for 'D2IMFILE' in the science header.
             If 'D2IMFILE' is not found and there is no d2im correction,
             it is set to 'NOMODEL'
             If d2im correction exists, but 'D2IMFILE' is missing from science
             header, it is set to 'UNKNOWN'
    author: string
            Name of user who created the headerlet, added as 'AUTHOR' keyword
            to headerlet PRIMARY header
    descrip: string
            Short description of the solution provided by the headerlet
            This description will be added as the single 'DESCRIP' keyword
            to the headerlet PRIMARY header
    history: filename, string or list of strings
            Long (possibly multi-line) description of the solution provided
            by the headerlet. These comments will be added as 'HISTORY' cards
            to the headerlet PRIMARY header
            If filename is specified, it will format and attach all text from
            that file as the history.
    logging: boolean
             enable file logging
    logmode: 'w' or 'a'
             log file open mode

    Returns
    -------
    Headerlet object
    """

    fobj, fname, close_file = parse_filename(filename)
    # Define extension to evaluate for verification of input parameters
    wcsext = 1
    if fu.isFits(fname)[1] == 'simple':
        wcsext = 0
    # Translate 'wcskey' value for PRIMARY WCS to valid altwcs value of ' '
    if wcskey == 'PRIMARY':
        wcskey = ' '
        logger.info("wcskey reset from 'PRIMARY' to ' '")
    wcskey = wcskey.upper()
    wcsnamekw = "".join(["WCSNAME", wcskey.upper()]).rstrip()
    hdrnamekw = "".join(["HDRNAME", wcskey.upper()]).rstrip()

    wnames = altwcs.wcsnames(fobj,ext=wcsext)

    if not wcsname:
        # User did not specify a value for 'wcsname'
        if wcsnamekw in fobj[wcsext].header:
            #check if there's a WCSNAME for this wcskey in the header
            wcsname = fobj[wcsext].header[wcsnamekw]
            logger.info("Setting wcsname from header[%s] to %s" % (wcsnamekw, wcsname))
        else:
            if hdrname not in ['', ' ', None, "INDEF"]:
                """
                If wcsname for this wcskey was not provided
                and WCSNAME<wcskey> does not exist in the header
                and hdrname is provided, then
                use hdrname as WCSNAME for the headerlet.
                """
                wcsname = hdrname
                logger.debug("Setting wcsname from hdrname to %s" % hdrname)
            else:
                if hdrnamekw in fobj[wcsext].header:
                    wcsname = fobj[wcsext].header[hdrnamekw]
                    logger.debug("Setting wcsname from header[%s] to %s" % (hdrnamekw, wcsname))
                else:
                    message = "Required keywords 'HDRNAME' or 'WCSNAME' not found!\n"
                    message += "Please specify a value for parameter 'hdrname',\n"
                    message += "  or update header with 'WCSNAME' keyword."
                    logger.critical(message)
                    raise KeyError
    else:
        # Verify that 'wcsname' and 'wcskey' values specified by user reference
        # the same WCS
        wname = fobj[wcsext].header[wcsnamekw]
        if wcsname != wname:
            message = "\tInconsistent values for 'wcskey' and 'wcsname' specified!\n"
            message += "    'wcskey' = %s and 'wcsname' = %s. \n" % (wcskey, wcsname)
            message += "Actual value of %s found to be %s. \n" % (wcsnamekw, wname)
            logger.critical(message)
            raise KeyError
    wkeys = altwcs.wcskeys(fobj, ext=wcsext)
    if wcskey != ' ':
        if wcskey not in wkeys:
            logger.critical('No WCS with wcskey=%s found in extension %s.  Skipping...' % (wcskey, str(wcsext)))
            raise ValueError

    # get remaining required keywords
    if destim is None:
        if 'ROOTNAME' in fobj[0].header:
            destim = fobj[0].header['ROOTNAME']
            logger.info("Setting destim to rootname of the science file")
        else:
            destim = fname
            logger.info('DESTIM not provided')
            logger.info('Keyword "ROOTNAME" not found')
            logger.info('Using file name as DESTIM')

    if not hdrname:
        # check if HDRNAME<wcskey> is in header
        if hdrnamekw in fobj[wcsext].header:
            hdrname = fobj[wcsext].header[hdrnamekw]
        else:
            if wcsnamekw in fobj[wcsext].header:
                hdrname = fobj[wcsext].header[wcsnamekw]
                message = """
                Using default value for HDRNAME of "%s" derived from %s.
                """ % (hdrname, wcsnamekw)
                logger.info(message)
                logger.info("Setting hdrname to %s from header[%s]"
                            % (hdrname, wcsnamekw))
            else:
                message = "Required keywords 'HDRNAME' or 'WCSNAME' not found"
                logger.critical(message)
                raise KeyError

    if not sipname:
        sipname = utils.build_sipname(fobj)
        logger.info("Setting sipname value to %s" % sipname)
    if not npolfile:
        npolfile = utils.build_npolname(fobj)
        logger.info("Setting npolfile value to %s" % npolfile)
    if not d2imfile:
        d2imfile = utils.build_d2imname(fobj)
        logger.info("Setting d2imfile value to %s" % d2imfile)
    distname = utils.build_distname(sipname, npolfile, d2imfile)
    logger.info("Setting distname to %s" % distname)
    rms_ra = get_header_kw_vals(fobj[wcsext].header,
                    ("RMS_RA"+wcskey).rstrip(), rms_ra, default=0)
    rms_dec = get_header_kw_vals(fobj[wcsext].header,
                    ("RMS_DEC"+wcskey).rstrip(), rms_dec, default=0)
    nmatch = get_header_kw_vals(fobj[wcsext].header,
                    ("NMATCH"+wcskey).rstrip(), nmatch, default=0)
    catalog = get_header_kw_vals(fobj[wcsext].header,
                    ("CATALOG"+wcskey).rstrip(), catalog, default="")

    # get the version of STWCS used to create the WCS of the science file.
    try:
        upwcsver = fobj[0].header.ascard['UPWCSVER']
    except KeyError:
        upwcsver = pyfits.Card("UPWCSVER", " ",
                               "Version of STWCS used to update the WCS")
    try:
        pywcsver = fobj[0].header.ascard['PYWCSVER']
    except KeyError:
        pywcsver = pyfits.Card("PYWCSVER", " ",
                               "Version of PYWCS used to update the WCS")

    if isinstance(sciext, int):
        sciext = [sciext] # allow for specification of simple FITS header
    elif isinstance(sciext, str):
        numsciext = countExtn(fobj, sciext)
        sciext = [(sciext + ", " + str(i)) for i in range(1, numsciext+1)]
    elif isinstance(sciext, list):
        pass
    else:
        errstr = "Expected sciext to be a list of FITS extensions with science data\n"+\
              "    a valid EXTNAME string, or an integer."
        logger.critical(errstr)
        raise ValueError

    if wcskey is 'O':
        message = "Warning: 'O' is a reserved key for the original WCS. Quitting..."
        logger.info(message)
        return

    # open file and parse comments
    if history not in ['', ' ', None, 'INDEF'] and os.path.isfile(history):
        f = open(fu.osfn(history))
        history = f.readlines()
        f.close()

    logger.debug("Data extensions from which to create headerlet:\n\t %s"
                 % (str(sciext)))
    hdul = pyfits.HDUList()
    phdu = _create_primary_HDU(destim, hdrname, distname, wcsname,
                             sipname, npolfile, d2imfile,
                             rms_ra, rms_dec, nmatch, catalog,
                             upwcsver, pywcsver,
                             author, descrip, history)
    hdul.append(phdu)
    orient_comment = "positions angle of image y axis (deg. e of n)"
    wcsdvarr_extns = []
    if fu.isFits(fobj)[1] is not 'simple':
        for e in sciext:
            try:
                fext = int(e)
            except ValueError:
                fext = fu.parseExtn(e)
            wkeys = altwcs.wcskeys(fobj, ext=fext)
            if wcskey != ' ':
                if wcskey not in wkeys:
                    logger.debug('No WCS with wcskey=%s found in extension %s.  Skipping...' % (wcskey, str(e)))
                    continue # skip any extension which does not have this wcskey

            # This reads in full model: alternate WCS keywords plus SIP
            hwcs = HSTWCS(fobj, ext=fext, wcskey=' ')

            h = hwcs.wcs2header(sip2hdr=True)
            if hasattr(hwcs, 'orientat'):
                h.update('ORIENTAT', hwcs.orientat, comment=orient_comment)
            h.update('RMS_RA', rms_ra,
                    comment='RMS in RA at ref pix of headerlet solution')
            h.update('RMS_DEC', rms_dec,
                    comment='RMS in Dec at ref pix of headerlet solution')
            h.update('NMATCH', nmatch,
                    comment='Number of sources used for headerlet solution')
            h.update('CATALOG', catalog,
                    comment='Astrometric catalog used for headerlet solution')

            if wcskey != ' ':
                # Now read in specified linear WCS terms from alternate WCS
                try:
                    althdr = altwcs.convertAltWCS(fobj, fext, oldkey=wcskey, newkey=" ")
                    althdrwcs = HSTWCS(fobj, fext, wcskey=wcskey)
                except KeyError:
                    continue # Skip over any extension which does not have a WCS
                althdr = althdr.ascard
                # Update full WCS with values from alternate WCS
                for card in althdr:
                    h.update(card.key, card.value)
                if hasattr(althdrwcs, 'orientat'):
                    h.update('ORIENTAT', althdrwcs.orientat, comment=orient_comment)
            h = h.ascard

            if hasattr(hwcs, 'vafactor'):
                h.append(pyfits.Card(key='VAFACTOR', value=hwcs.vafactor,
                                 comment='Velocity aberration plate scale factor'))
            h.insert(0, pyfits.Card(key='EXTNAME', value='SIPWCS',
                                    comment='Extension name'))
            if isinstance(fext, int):
                if 'extver' in fobj[fext].header:
                    val = fobj[fext].header['extver']
                else:
                    val = fext
            else: val = fext[1]
            h.insert(1, pyfits.Card(key='EXTVER', value=val,
                                    comment='Extension version'))
            h.append(pyfits.Card(key="SCIEXT", value=e,
                                 comment="Target science data extension"))
            fhdr = fobj[fext].header.ascard
            if npolfile is not 'NOMODEL':
                cpdis = fhdr['CPDIS*...']
                for c in range(1, len(cpdis) + 1):
                    h.append(cpdis[c - 1])
                    dp = fhdr['DP%s*...' % c]
                    for kw in dp:
                        dpval = kw.value
                        if 'EXTVER' in kw.key:
                            wcsdvarr_extns.append(dpval)
                            break

                    h.extend(dp)
                    try:
                        h.append(fhdr['CPERROR%s' % c])
                    except KeyError:
                        pass

                try:
                    h.append(fhdr['NPOLEXT'])
                except KeyError:
                    pass

            if d2imfile is not 'NOMODEL':
                try:
                    h.append(fhdr['D2IMEXT'])
                except KeyError:
                    pass

                try:
                    h.append(fhdr['AXISCORR'])
                except KeyError:
                    logger.critical("'D2IMFILE' kw exists but keyword 'AXISCORR' was not found in "
                                     "%s['SCI',%d]" % (fname, val))
                    raise
                try:
                    h.append(fhdr['D2IMERR'])
                except KeyError:
                    h.append(pyfits.Card(key='DPERROR', value=0,
                                         comment='Maximum error of D2IMARR'))

            hdu = pyfits.ImageHDU(header=pyfits.Header(h))
            hdul.append(hdu)

    for w in wcsdvarr_extns:
        hdu = fobj[('WCSDVARR', w)].copy()
        hdul.append(hdu)
    numd2im = countExtn(fobj, 'D2IMARR')
    for d in range(1, numd2im + 1):
        hdu = fobj[('D2IMARR', d)].copy()
        hdul.append(hdu)

    if close_file:
        fobj.close()
    return Headerlet(hdul, logging=logging, logmode='a')


@with_logging
def apply_headerlet_as_primary(filename, hdrlet, attach=True, archive=True,
                                force=False, logging=False, logmode='a'):
    """
    Apply headerlet 'hdrfile' to a science observation 'destfile' as the primary WCS

    Parameters
    ----------
    filename: string
             File name of science observation whose WCS solution will be updated
    hdrlet: string
             Headerlet file
    attach: boolean
            True (default): append headerlet to FITS file as a new extension.
    archive: boolean
            True (default): before updating, create a headerlet with the
            WCS old solution.
    force: boolean
            If True, this will cause the headerlet to replace the current PRIMARY
            WCS even if it has a different distortion model. [Default: False]
    logging: boolean
            enable file logging
    logmode: 'w' or 'a'
             log file open mode
    """

    hlet = Headerlet(hdrlet, logging=logging)
    hlet.apply_as_primary(filename, attach=attach, archive=archive,
                          force=force)


@with_logging
def apply_headerlet_as_alternate(filename, hdrlet, attach=True, wcskey=None,
                                wcsname=None, logging=False, logmode='w'):
    """
    Apply headerlet to a science observation as an alternate WCS

    Parameters
    ----------
    filename: string
             File name of science observation whose WCS solution will be updated
    hdrlet: string
             Headerlet file
    attach: boolean
          flag indicating if the headerlet should be attached as a
          HeaderletHDU to fobj. If True checks that HDRNAME is unique
          in the fobj and stops if not.
    wcskey: string
          Key value (A-Z, except O) for this alternate WCS
          If None, the next available key will be used
    wcsname: string
          Name to be assigned to this alternate WCS
          WCSNAME is a required keyword in a Headerlet but this allows the
          user to change it as desired.
    logging: boolean
          enable file logging
    logmode: 'a' or 'w'
    """

    hlet = Headerlet(hdrlet, logging=logging, logmode=logmode)
    hlet.apply_as_alternate(filename, attach=attach,
                            wcsname=wcsname, wcskey=wcskey)


@with_logging
def attach_headerlet(filename, hdrlet, logging=False, logmode='a'):
    """
    Attach Headerlet as an HeaderletHDU to a science file

    Parameters
    ----------
    filename: string, HDUList
            science file to which the headerlet should be applied
    hdrlet: string or Headerlet object
            string representing a headerlet file
    logging: boolean
            enable file logging
    logmode: 'a' or 'w'
    """

    hlet = Headerlet(hdrlet, logging=logging, logmode='a')
    hlet.attach_to_file(filename)


@with_logging
def delete_headerlet(filename, hdrname=None, hdrext=None, distname=None,
                     logging=False, logmode='w'):
    """
    Deletes HeaderletHDU(s) from a science file

    Notes
    -----
    One of hdrname, hdrext or distname should be given.
    If hdrname is given - delete a HeaderletHDU with a name HDRNAME from fobj.
    If hdrext is given - delete HeaderletHDU in extension.
    If distname is given - deletes all HeaderletHDUs with a specific distortion model from fobj.
    Updates wcscorr

    Parameters
    ----------
    filename: string or HDUList
           Either a filename or PyFITS HDUList object for the input science file
            An input filename (str) will be expanded as necessary to interpret
            any environmental variables included in the filename.
    hdrname: string or None
        HeaderletHDU primary header keyword HDRNAME
    hdrext: int, tuple or None
        HeaderletHDU FITS extension number
        tuple has the form ('HDRLET', 1)
    distname: string or None
        distortion model as specified in the DISTNAME keyword
    logging: boolean
             enable file logging
    logmode: 'a' or 'w'
    """

    hdrlet_ind = find_headerlet_HDUs(filename, hdrname=hdrname, hdrext=hdrext,
                            distname=distname, logging=logging, logmode='a')
    if len(hdrlet_ind) == 0:
        message = """
        No HDUs deleted... No Headerlet HDUs found with '
        hdrname = %s
        hdrext  = %s
        distname = %s
        Please review input parameters and try again.
        """ % (hdrname, str(hdrext), distname)
        logger.critical(message)
        return

    fobj, fname, close_fobj = parse_filename(filename, mode='update')

    # delete row(s) from WCSCORR table now...
    #
    #
    if hdrname not in ['', ' ', None, 'INDEF']:
        selections = {'hdrname': hdrname}
    elif hdrname in ['', ' ', None, 'INDEF'] and hdrext is not None:
        selections = {'hdrname': fobj[hdrext].header['hdrname']}
    else:
        selections = {'distname': distname}
    wcscorr.delete_wcscorr_row(fobj['WCSCORR'].data, selections)

    # delete the headerlet extension now
    for hdrind in hdrlet_ind:
        del fobj[hdrind]

    # Update file object with changes
    fobj.flush()
    # close file, if was opened by this function
    if close_fobj:
        fobj.close()
    logger.critical('Deleted headerlet from extension(s) %s ' % str(hdrlet_ind))


def headerlet_summary(filename, columns=None, pad=2, maxwidth=None,
                      output=None, clobber=True, quiet=False):
    """
    Print a summary of all HeaderletHDUs in a science file to STDOUT, and
    optionally to a text file
    The summary includes:
        HDRLET_ext_number  HDRNAME  WCSNAME DISTNAME SIPNAME NPOLFILE D2IMFILE

    Parameters
    ----------
    filename: string or HDUList
           Either a filename or PyFITS HDUList object for the input science file
            An input filename (str) will be expanded as necessary to interpret
            any environmental variables included in the filename.
    columns: list
        List of headerlet PRIMARY header keywords to report in summary
        By default (set to None), it will use the default set of keywords
        defined as the global list DEFAULT_SUMMARY_COLS
    pad: int
        Number of padding spaces to put between printed columns
        [Default: 2]
    maxwidth: int
        Maximum column width(not counting padding) for any column in summary
        By default (set to None), each column's full width will be used
    output: string (optional)
        Name of optional output file to record summary. This filename
        can contain environment variables.
        [Default: None]
    clobber: bool
        If True, will overwrite any previous output file of same name
    quiet: bool
        If True, will NOT report info to STDOUT
    """
    if columns is None:
        summary_cols = DEFAULT_SUMMARY_COLS
    else:
        summary_cols = columns

    summary_dict = {}
    for kw in summary_cols:
        summary_dict[kw] = copy.deepcopy(COLUMN_DICT)

    # Define Extension number column
    extnums_col = copy.deepcopy(COLUMN_DICT)
    extnums_col['name'] = 'EXTN'
    extnums_col['width'] = 6

    fobj, fname, close_fobj = parse_filename(filename)
    # find all HDRLET extensions and combine info into a single summary
    for extn in fobj:
        if 'extname' in extn.header and extn.header['extname'] == 'HDRLET':
            hdrlet_indx = fobj.index_of(('hdrlet', extn.header['extver']))
            try:
                ext_cols, ext_summary = extn.headerlet.summary(columns=summary_cols)
                extnums_col['vals'].append(hdrlet_indx)
                for kw in summary_cols:
                    for key in COLUMN_DICT:
                        summary_dict[kw][key].extend(ext_summary[kw][key])
            except:
                print "Skipping headerlet"
                print "Could not read Headerlet from extension ", hdrlet_indx

    if close_fobj:
        fobj.close()

    # Print out the summary dictionary
    print_summary(summary_cols, summary_dict, pad=pad, maxwidth=maxwidth,
                    idcol=extnums_col, output=output,
                    clobber=clobber, quiet=quiet)


@with_logging
def restore_from_headerlet(filename, hdrname=None, hdrext=None, archive=True,
                           force=False, logging=False, logmode='w'):
    """
    Restores a headerlet as a primary WCS

    Parameters
    ----------
    filename: string or HDUList
           Either a filename or PyFITS HDUList object for the input science file
            An input filename (str) will be expanded as necessary to interpret
            any environmental variables included in the filename.
    hdrname: string
        HDRNAME keyword of HeaderletHDU
    hdrext: int or tuple
        Headerlet extension number of tuple ('HDRLET',2)
    archive: boolean (default: True)
        When the distortion model in the headerlet is the same as the distortion model of
        the science file, this flag indicates if the primary WCS should be saved as an alternate
        nd a headerlet extension.
        When the distortion models do not match this flag indicates if the current primary and
        alternate WCSs should be archived as headerlet extensions and alternate WCS.
    force: boolean (default:False)
        When the distortion models of the headerlet and the primary do not match, and archive
        is False, this flag forces an update of the primary.
    logging: boolean
           enable file logging
    logmode: 'a' or 'w'
    """

    hdrlet_ind = find_headerlet_HDUs(filename, hdrext=hdrext, hdrname=hdrname)

    fobj, fname, close_fobj = parse_filename(filename, mode='update')

    if len(hdrlet_ind) > 1:
        if hdrext:
            kwerr = 'hdrext'
            kwval = hdrext
        else:
            kwerr = 'hdrname'
            kwval = hdrname
        message = """
        Multiple Headerlet extensions found with the same name.
        %d Headerlets with "%s" = %s found in %s.
        """% (len(hdrlet_ind), kwerr, kwval, fname)
        if close_fobj:
            fobj.close()
        logger.critical(message)
        raise ValueError

    hdrlet_indx = hdrlet_ind[0]

    # read headerlet from HeaderletHDU into memory
    if hasattr(fobj[hdrlet_ind[0]], 'hdulist'):
        hdrlet = fobj[hdrlet_indx].hdulist
    else:
        hdrlet = fobj[hdrlet_indx].headerlet # older convention in PyFITS

    # read in the names of the extensions which HeaderletHDU updates
    extlist = []
    for ext in hdrlet:
        if 'extname' in ext.header and ext.header['extname'] == 'SIPWCS':
            # convert from string to tuple or int
            sciext = eval(ext.header['sciext'])
            extlist.append(fobj[sciext])
    # determine whether distortion is the same
    current_distname = hdrlet[0].header['distname']
    same_dist = True
    if current_distname != fobj[0].header['distname']:
        same_dist = False
        if not archive and not force:
            if close_fobj:
                fobj.close()
            message = """
            Headerlet does not have the same distortion as image!
            Set "archive"=True to save old distortion model, or
            set "force"=True to overwrite old model with new.
            """
            logger.critical(message)
            raise ValueError

    # check whether primary WCS has been archived already
    # Use information from first 'SCI' extension
    priwcs_name = None

    scihdr = extlist[0].header
    sci_wcsnames = altwcs.wcsnames(scihdr).values()
    if 'hdrname' in scihdr:
        priwcs_hdrname = scihdr['hdrname']
    else:
        if 'wcsname' in scihdr:
            priwcs_hdrname = priwcs_name = scihdr['wcsname']
        else:
            if 'idctab' in scihdr:
                priwcs_hdrname = ''.join(['IDC_',
                        utils.extract_rootname(scihdr['idctab'], suffix='_idc')])
            else:
                priwcs_hdrname = 'UNKNOWN'
            priwcs_name = priwcs_hdrname
            scihdr.update('WCSNAME', priwcs_name)

    priwcs_unique = verify_hdrname_is_unique(fobj, priwcs_hdrname)
    if archive and priwcs_unique:
        if priwcs_unique:
            newhdrlet = create_headerlet(fobj, sciext=scihdr['extname'],
                        hdrname=priwcs_hdrname)
            newhdrlet.attach_to_file(fobj)
    #
    # copy hdrlet as a primary
    #
    hdrlet.apply_as_primary(fobj, attach=False, archive=archive, force=force)

    fobj.flush()
    if close_fobj:
        fobj.close()


@with_logging
def restore_all_with_distname(filename, distname, primary, archive=True,
                              sciext='SCI', logging=False, logmode='w'):
    """
    Restores all HeaderletHDUs with a given distortion model as alternate WCSs and a primary

    Parameters
    --------------
    filename: string or HDUList
           Either a filename or PyFITS HDUList object for the input science file
            An input filename (str) will be expanded as necessary to interpret
            any environmental variables included in the filename.
    distname: string
        distortion model as represented by a DISTNAME keyword
    primary: int or string or None
        HeaderletHDU to be restored as primary
        if int - a fits extension
        if string - HDRNAME
        if None - use first HeaderletHDU
    archive: boolean (default True)
        flag indicating if HeaderletHDUs should be created from the
        primary and alternate WCSs in fname before restoring all matching
        headerlet extensions
    logging: boolean
         enable file logging
    logmode: 'a' or 'w'
    """

    fobj, fname, close_fobj = parse_filename(filename, mode='update')

    hdrlet_ind = find_headerlet_HDUs(fobj, distname=distname)
    if len(hdrlet_ind) == 0:
        message = """
        No Headerlet extensions found with

        DISTNAME = %s in %s.

        For a full list of DISTNAMEs found in all headerlet extensions:

        get_headerlet_kw_names(fobj, kw='DISTNAME')
        """ % (distname, fname)
        if close_fobj:
            fobj.close()
        logger.critical(message)
        raise ValueError

    # Interpret 'primary' parameter input into extension number
    if primary is None:
        primary_ind = hdrlet_ind[0]
    elif isinstance(primary, int):
        primary_ind = primary
    else:
        primary_ind = None
        for ind in hdrlet_ind:
            if fobj[ind].header['hdrname'] == primary:
                primary_ind = ind
                break
        if primary_ind is None:
            if close_fobj:
                fobj.close()
            message = """
            No Headerlet extensions found with DISTNAME = %s in %s.
            """ % (primary, fname)
            logger.critical(message)
            raise ValueError
    # Check to see whether 'primary' HeaderletHDU has same distname as user
    # specified on input

    # read headerlet from HeaderletHDU into memory
    if hasattr(fobj[primary_ind], 'hdulist'):
        primary_hdrlet = fobj[primary_ind].hdulist
    else:
        primary_hdrlet = fobj[primary_ind].headerlet # older convention in PyFITS
    pri_distname = primary_hdrlet[0].header['distname']
    if pri_distname != distname:
        if close_fobj:
            fobj.close()
        message = """
        Headerlet extension to be used as PRIMARY WCS
        has "DISTNAME" = %s
        "DISTNAME" = %s was specified on input.
        All updated WCSs must have same DISTNAME. Quitting...'
        """ % (pri_distname, distname)
        logger.critical(message)
        raise ValueError

    # read in the names of the WCSs which the HeaderletHDUs will update
    wnames = altwcs.wcsnames(fobj[sciext, 1].header)

    # work out how many HeaderletHDUs will be used to update the WCSs
    numhlt = len(hdrlet_ind)
    hdrnames = get_headerlet_kw_names(fobj, kw='wcsname')

    # read in headerletHDUs and update WCS keywords
    for hlet in hdrlet_ind:
        if fobj[hlet].header['distname'] == distname:
            if hasattr(fobj[hlet], 'hdulist'):
                hdrlet = fobj[hlet].hdulist
            else:
                hdrlet = fobj[hlet].headerlet # older convention in PyFITS
            if hlet == primary_ind:
                hdrlet.apply_as_primary(fobj, attach=False,
                                        archive=archive, force=True)
            else:
                hdrlet.apply_as_alternate(fobj, attach=False,
                                          wcsname=hdrlet[0].header['wcsname'])

    fobj.flush()
    if close_fobj:
        fobj.close()


@with_logging
def archive_as_headerlet(filename, hdrname, sciext='SCI',
                        wcsname=None, wcskey=None, destim=None,
                        sipname=None, npolfile=None, d2imfile=None,
                        author=None, descrip=None, history=None,
                        rms_ra=None, rms_dec=None, nmatch=None, catalog=None,
                        logging=False, logmode='w'):
    """
    Save a WCS as a headerlet extension and write it out to a file.

    This function will create a headerlet, attach it as an extension to the
    science image (if it has not already been archived) then, optionally,
    write out the headerlet to a separate headerlet file.

    Either wcsname or wcskey must be provided, if both are given, they must match a valid WCS
    Updates wcscorr if necessary.

    Parameters
    ----------
    filename: string or HDUList
           Either a filename or PyFITS HDUList object for the input science file
            An input filename (str) will be expanded as necessary to interpret
            any environmental variables included in the filename.
    hdrname: string
        Unique name for this headerlet, stored as HDRNAME keyword
    sciext: string
        name (EXTNAME) of extension that contains WCS to be saved
    wcsname: string
        name of WCS to be archived, if " ": stop
    wcskey: one of A...Z or " " or "PRIMARY"
        if " " or "PRIMARY" - archive the primary WCS
    destim: string
        DESTIM keyword
        if  NOne, use ROOTNAME or science file name
    sipname: string or None (default)
             Name of unique file where the polynomial distortion coefficients were
             read from. If None, the behavior is:
             The code looks for a keyword 'SIPNAME' in the science header
             If not found, for HST it defaults to 'IDCTAB'
             If there is no SIP model the value is 'NOMODEL'
             If there is a SIP model but no SIPNAME, it is set to 'UNKNOWN'
    npolfile: string or None (default)
             Name of a unique file where the non-polynomial distortion was stored.
             If None:
             The code looks for 'NPOLFILE' in science header.
             If 'NPOLFILE' was not found and there is no npol model, it is set to 'NOMODEL'
             If npol model exists, it is set to 'UNKNOWN'
    d2imfile: string
             Name of a unique file where the detector to image correction was
             stored. If None:
             The code looks for 'D2IMFILE' in the science header.
             If 'D2IMFILE' is not found and there is no d2im correction,
             it is set to 'NOMODEL'
             If d2im correction exists, but 'D2IMFILE' is missing from science
             header, it is set to 'UNKNOWN'
    author: string
            Name of user who created the headerlet, added as 'AUTHOR' keyword
            to headerlet PRIMARY header
    descrip: string
            Short description of the solution provided by the headerlet
            This description will be added as the single 'DESCRIP' keyword
            to the headerlet PRIMARY header
    history: filename, string or list of strings
            Long (possibly multi-line) description of the solution provided
            by the headerlet. These comments will be added as 'HISTORY' cards
            to the headerlet PRIMARY header
            If filename is specified, it will format and attach all text from
            that file as the history.
    logging: boolean
            enable file folling
    logmode: 'w' or 'a'
             log file open mode
    """

    fobj, fname, close_fobj = parse_filename(filename, mode='update')

    if wcsname in [None, ' ', '', 'INDEF'] and wcskey is None:
        message = """
        No valid WCS found found in %s.
        A valid value for either "wcsname" or "wcskey"
        needs to be specified.
        """ % fname
        if close_fobj:
            fobj.close()
        logger.critical(message)
        raise ValueError

    # Translate 'wcskey' value for PRIMARY WCS to valid altwcs value of ' '
    if wcskey == 'PRIMARY':
        wcskey = ' '
    wcskey = wcskey.upper()

    numhlt = countExtn(fobj, 'HDRLET')

    if wcsname is None:
        scihdr = fobj[sciext, 1].header
        wcsname = scihdr['wcsname'+wcskey]

    if hdrname in [None, ' ', '']:
        hdrname = wcsname

    # Check to see whether or not a HeaderletHDU with this hdrname already
    # exists
    hdrnames = get_headerlet_kw_names(fobj)
    if hdrname not in hdrnames:
        hdrletobj = create_headerlet(fobj, sciext=sciext,
                                    wcsname=wcsname, wcskey=wcskey,
                                    hdrname=hdrname,
                                    sipname=sipname, npolfile=npolfile,
                                    d2imfile=d2imfile, author=author,
                                    descrip=descrip, history=history,
                                    rms_ra=rms_ra, rms_dec=rms_dec,
                                    nmatch=nmatch, catalog=catalog,
                                    logging=False)
        hlt_hdu = HeaderletHDU.fromheaderlet(hdrletobj)

        if destim is not None:
            hlt_hdu[0].header['destim'] = destim

        fobj.append(hlt_hdu)

        fobj.flush()
    else:
        message = """
        Headerlet with hdrname %s already archived for WCS %s
        No new headerlet appended to %s .
        """ % (hdrname, wcsname, fname)
        logger.critical(message)

    if close_fobj:
        fobj.close()

#### Headerlet Class definitions
class Headerlet(pyfits.HDUList):
    """
    A Headerlet class
    Ref: http://mediawiki.stsci.edu/mediawiki/index.php/Telescopedia:Headerlets
    """

    def __init__(self, fobj, mode='copyonwrite', logging=False, logmode='w'):
        """
        Parameters
        ----------
        fobj:  string
                Name of headerlet file, file-like object, a list of HDU
                instances, or an HDUList instance
        mode: string, optional
                Mode with which to open the given file object
        logging: boolean
                 enable file logging
        logmode: 'w' or 'a'
                for internal use only, indicates whether the log file
                should be open in attach or write mode
        """
        self.logging = logging
        init_logging('class Headerlet', level=logging, mode=logmode)

        fobj, fname, close_file = parse_filename(fobj)

        super(Headerlet, self).__init__(fobj)
        self.fname = self.filename()
        self.hdrname = self[0].header["HDRNAME"]
        self.wcsname = self[0].header["WCSNAME"]
        self.upwcsver = self[0].header.get("UPWCSVER", "")
        self.pywcsver = self[0].header.get("PYWCSVER", "")
        self.destim = self[0].header["DESTIM"]
        self.sipname = self[0].header["SIPNAME"]
        self.npolfile = self[0].header["NPOLFILE"]
        self.d2imfile = self[0].header["D2IMFILE"]
        self.distname = self[0].header["DISTNAME"]
        self.vafactor = self[1].header.get("VAFACTOR", 1) #None instead of 1?
        self.author = self[0].header["AUTHOR"]
        self.descrip = self[0].header["DESCRIP"]

        self.fit_kws = ['HDRNAME', 'RMS_RA', 'RMS_DEC', 'NMATCH', 'CATALOG']
        self.history = ''
        for card in self[0].header['HISTORY*']:
            self.history += card.value+'\n'

        self.d2imerr = 0
        self.axiscorr = 1

    def apply_as_primary(self, fobj, attach=True, archive=True, force=False):
        """
        Copy this headerlet as a primary WCS to fobj

        Parameters
        ----------
        fobj: string, HDUList
              science file to which the headerlet should be applied
        attach: boolean
              flag indicating if the headerlet should be attached as a
              HeaderletHDU to fobj. If True checks that HDRNAME is unique
              in the fobj and stops if not.
        archive: boolean (default is True)
              When the distortion model in the headerlet is the same as the
              distortion model of the science file, this flag indicates if
              the primary WCS should be saved as an alternate and a headerlet
              extension.
              When the distortion models do not match this flag indicates if
              the current primary and alternate WCSs should be archived as
              headerlet extensions and alternate WCS.
        force: boolean (default is False)
              When the distortion models of the headerlet and the primary do
              not match, and archive is False this flag forces an update
              of the primary
        """
        self.hverify()
        fobj, fname, close_dest = parse_filename(fobj, mode='update')
        if self.verify_dest(fobj):

            # Check to see whether the distortion model in the destination
            # matches the distortion model in the headerlet being applied
            dist_models_equal = True
            if  self[0].header['DISTNAME'] != fobj[0].header['DISTNAME']:
                if self.logging:
                    message = """
                    Distortion model in headerlet not the same as destination model
                    Headerlet model  : %s
                    Destination model: %s
                    """ % (self[0].header['DISTNAME'], fobj[0].header['DISTNAME'])
                    logger.critical(message)
                dist_models_equal = False

            if not dist_models_equal and not force:
                raise ValueError

            orig_hlt_hdu = None
            numhlt = countExtn(fobj, 'HDRLET')
            hdrlet_extnames = get_headerlet_kw_names(fobj)

            # Insure that WCSCORR table has been created with all original
            # WCS's recorded prior to adding the headerlet WCS
            wcscorr.init_wcscorr(fobj)

            alt_hlethdu = []
            # If archive has been specified
            #   regardless of whether or not the distortion models are equal...
            if archive:

                if 'wcsname' in fobj[('SCI', 1)].header:
                    hdrname = fobj[('SCI', 1)].header['WCSNAME']
                    wcsname = hdrname
                else:
                    hdrname = fobj[0].header['ROOTNAME'] + '_orig'
                    wcsname = None
                wcskey = ' '
                # Check the HDRNAME for all current headerlet extensions
                # to see whether this PRIMARY WCS has already been appended
                wcsextn = self[1].header['SCIEXT']
                try:
                    wcsextn = int(wcsextn)
                except ValueError:
                    wcsextn = fu.parseExtn(wcsextn)

                if hdrname not in hdrlet_extnames:
                    # -  if WCS has not been saved, write out WCS as headerlet extension
                    # Create a headerlet for the original Primary WCS data in the file,
                    # create an HDU from the original headerlet, and append it to
                    # the file
                    orig_hlt = create_headerlet(fobj, sciext=wcsextn[0],
                                    wcsname=wcsname, wcskey=wcskey,
                                    hdrname=hdrname, sipname=None,
                                    npolfile=None, d2imfile=None,
                                    author=None, descrip=None, history=None,
                                    logging=self.logging)
                    orig_hlt_hdu = HeaderletHDU.fromheaderlet(orig_hlt)
                    numhlt += 1
                    orig_hlt_hdu.header.update('EXTVER', numhlt)

                if dist_models_equal:
                    # Use the WCSNAME to determine whether or not to archive
                    # Primary WCS as altwcs
                    # wcsname = hwcs.wcs.name
                    scihdr = fobj[wcsextn].header
                    if 'hdrname' in scihdr:
                        priwcs_name = scihdr['hdrname']
                    else:
                        if 'wcsname' in scihdr:
                            priwcs_name = scihdr['wcsname']
                        else:
                            if 'idctab' in scihdr:
                                priwcs_name = ''.join(['IDC_',
                                    utils.extract_rootname(scihdr['idctab'],
                                                            suffix='_idc')])
                            else:
                                priwcs_name = 'UNKNOWN'
                    nextkey = altwcs.next_wcskey(fobj, ext=wcsextn)
                    numsci = countExtn(fobj, 'SCI')
                    sciext_list = []
                    for i in range(1, numsci+1):
                        sciext_list.append(('SCI', i))
                    altwcs.archiveWCS(fobj, ext=sciext_list, wcskey=nextkey,
                                      wcsname=priwcs_name)
                else:
                    for hname in altwcs.wcsnames(fobj, ext=wcsextn).values():
                        if hname != 'OPUS' and hname not in hdrlet_extnames:
                            # get HeaderletHDU for alternate WCS as well
                            alt_hlet = create_headerlet(fobj, sciext='SCI',
                                    wcsname=hname, wcskey=wcskey,
                                    hdrname=hname, sipname=None,
                                    npolfile=None, d2imfile=None,
                                    author=None, descrip=None, history=None,
                                    logging=self.logging)
                            numhlt += 1
                            alt_hlet_hdu = HeaderletHDU.fromheaderlet(alt_hlet)
                            alt_hlet_hdu.header.update('EXTVER', numhlt)
                            alt_hlethdu.append(alt_hlet_hdu)
                            hdrlet_extnames.append(hname)

            if not dist_models_equal:
                self._del_dest_WCS(fobj)
                #! Always attach these extensions last.
                # Otherwise their headers may get updated with the other WCS kw.
                numwdvar = countExtn(self, 'WCSDVARR')
                numd2im = countExtn(self, 'D2IMARR')
                for idx in range(1, numwdvar + 1):
                    fobj.append(self[('WCSDVARR', idx)].copy())
                for idx in range(1, numd2im + 1):
                    fobj.append(self[('D2IMARR', idx)].copy())

            refs = update_ref_files(self[0].header, fobj[0].header)
            numsip = countExtn(self, 'SIPWCS')
            for idx in range(1, numsip + 1):
                fhdr = fobj[('SCI', idx)].header
                siphdr = self[('SIPWCS', idx)].header.ascard

                if dist_models_equal:
                    hwcs = HSTWCS(fobj, ext=('SCI', idx))
                    hwcshdr = hwcs.wcs2header(sip2hdr=not(dist_models_equal))

                # a minimal attempt to get the position of the WCS keywords group
                # in the header by looking for the PA_APER kw.
                # at least make sure the WCS kw are written before the HISTORY kw
                # if everything fails, append the kw to the header
                akeywd = None
                bkeywd = None
                if 'PA_APER' in fhdr:
                    akeywd = 'PA_APER'
                else:
                    if 'HISTORY' in fhdr:
                        bkeywd = 'HISTORY'
                logger.debug(
                    "Updating WCS keywords after %s and/or before %s " %
                    (akeywd,bkeywd))
                update_cpdis = False
                for k in siphdr[-1::-1]:
                    # Replace or add WCS keyword from headerlet as PRIMARY WCS
                    # In the case that the distortion models are not equal,
                    # this will copy all keywords from headerlet into fobj
                    # When the distortion models are equal, though, it will
                    # only copy the primary WCS keywords (CRVAL,CRPIX,...)
                    if (dist_models_equal and (k.key in hwcshdr)) or \
                     (not dist_models_equal and k.key not in FITS_STD_KW):
                        if 'DP' not in k.key:
                            fhdr.update(k.key, k.value, comment=k.comment,
                                        after=akeywd, before=bkeywd)
                        else:
                            update_cpdis = True
                    else:
                        pass
                # Update WCS with HDRNAME as well
                for kw in self.fit_kws:
                    fhdr.update(kw, self[0].header[kw], after='WCSNAME')

                # Update header with record-valued keywords here
                if update_cpdis:
                    numdp = len(siphdr['CPDIS*'])
                    for dpaxis in range(1, numdp+1):
                        cpdis_indx = fhdr.ascard.index_of('CPDIS%d' % (dpaxis))
                        for dpcard in siphdr['DP%d*' % (dpaxis)][-1::-1]:
                            fhdr.ascard.insert(cpdis_indx, dpcard)

            # Update the WCSCORR table with new rows from the headerlet's WCSs
            wcscorr.update_wcscorr(fobj, self, 'SIPWCS')

            # Append the original headerlet
            if archive and orig_hlt_hdu:
                fobj.append(orig_hlt_hdu)
            # Append any alternate WCS Headerlets
            if len(alt_hlethdu) > 0:
                for ahdu in alt_hlethdu:
                    fobj.append(ahdu)
            if attach:
                # Finally, append an HDU for this headerlet
                self.attach_to_file(fobj)
            if close_dest:
                fobj.close()
        else:
            logger.critical("Observation %s cannot be updated with headerlet "
                            "%s" % (fname, self.hdrname))

    def apply_as_alternate(self, fobj, attach=True, wcskey=None, wcsname=None):
        """
        Copy this headerlet as an alternate WCS to fobj

        Parameters
        ----------
        fobj: string, HDUList
              science file/HDUList to which the headerlet should be applied
        attach: boolean
              flag indicating if the headerlet should be attached as a
              HeaderletHDU to fobj. If True checks that HDRNAME is unique
              in the fobj and stops if not.
        wcskey: string
              Key value (A-Z, except O) for this alternate WCS
              If None, the next available key will be used
        wcsname: string
              Name to be assigned to this alternate WCS
              WCSNAME is a required keyword in a Headerlet but this allows the
              user to change it as desired.

                    """
        self.hverify()
        wcskey = wcskey.upper()
        fobj, fname, close_dest = parse_filename(fobj, mode='update')
        if self.verify_dest(fobj):

            # Verify whether this headerlet has the same distortion found in
            # the image being updated
            if 'DISTNAME' in fobj[0].header:
                distname = fobj[0].header['DISTNAME']
            else:
                # perhaps call 'updatewcs.utils.construct_distname()' instead
                distname = 'UNKNOWN'

            if distname == 'UNKNOWN' or self.distname != distname:
                message = """
                Observation %s cannot be updated with headerlet %s
                Distortion in image:  %s \n    did not match \n headerlet distortion: %s
                The method .attach_to_file() can be used to append this headerlet to %s"
                """ % (fname, self.hdrname, distname, self.distname, fname)
                if close_dest:
                    fobj.close()
                logger.critical(message)
                raise ValueError

            # Insure that WCSCORR table has been created with all original
            # WCS's recorded prior to adding the headerlet WCS
            wcscorr.init_wcscorr(fobj)

            # determine value of WCSNAME to be used
            if wcsname is not None:
                wname = wcsname
            else:
                wname = self[0].header['WCSNAME']

            sciext = self[('SIPWCS', 1)].header['SCIEXT']
            try:
                sciext = int(sciext)
            except ValueError:
                sciext = fu.parseExtn(sciext)
            # determine what alternate WCS this headerlet will be assigned to
            if wcskey is None:
                wkey = altwcs.next_wcskey(fobj[sciext].header)
            else:
                available_keys = altwcs.available_wcskeys(fobj[sciext].header)
                if wcskey in available_keys:
                    wkey = wcskey
                else:
                    mess = "Observation %s already contains alternate WCS with key %s" % (fname, wcskey)
                    logger.critical(mess)
                    if close_dest:
                        fobj.close()
                    raise ValueError(mess)

            #numhlt = countExtn(fobj, 'HDRLET')
            numsip = countExtn(self, 'SIPWCS')
            for idx in range(1, numsip + 1):
                sciext = self[('SIPWCS', idx)].header['SCIEXT']
                try:
                    sciext = int(sciext)
                except ValueError:
                    sciext = fu.parseExtn(sciext)
                fhdr = fobj[sciext].header
                siphdr = self[('SIPWCS', idx)].header.ascard

                # a minimal attempt to get the position of the WCS keywords group
                # in the header by looking for the PA_APER kw.
                # at least make sure the WCS kw are written before the HISTORY kw
                # if everything fails, append the kw to the header
                try:
                    wind = fhdr.ascard.index_of('HISTORY')
                except KeyError:
                    wind = len(fhdr)
                logger.debug("Inserting WCS keywords at index %s" % wind)

                for k in siphdr:
                    for akw in altwcs.altwcskw:
                        if akw in k.key:
                            fhdr.ascard.insert(wind, pyfits.Card(
                                            key=k.key[:7]+wkey, value=k.value,
                                            comment=k.comment))
                    else:
                        pass

                fhdr.ascard.insert(wind, pyfits.Card('WCSNAME'+wkey, wname))
                # also update with HDRNAME (a non-WCS-standard kw)
                for kw in self.fit_kws:
                    fhdr.ascard.insert(wind, pyfits.Card(kw+wkey,
                                        self[0].header[kw]))
            # Update the WCSCORR table with new rows from the headerlet's WCSs
            wcscorr.update_wcscorr(fobj, self, 'SIPWCS')

            if attach:
                # Finally, append an HDU for this headerlet
                self.attach_to_file(fobj)
        else:
            mess = "Observation %s cannot be updated with headerlet %s" % (fname, self.hdrname)
            logger.critical(mess)
        if close_dest:
            fobj.close()

    def attach_to_file(self, fobj):
        """
        Attach Headerlet as an HeaderletHDU to a science file

        Parameters
        ----------
        fobj: string, HDUList
              science file/HDUList to which the headerlet should be applied

        Notes
        -----
        The algorithm used by this method:
        - verify headerlet can be applied to this file (based on DESTIM)
        - verify that HDRNAME is unique for this file
        - attach as HeaderletHDU to fobj
        - update wcscorr
        """
        self.hverify()
        fobj, fname, close_dest = parse_filename(fobj, mode='update')
        destver = self.verify_dest(fobj)
        hdrver = self.verify_hdrname(fobj)
        if destver and hdrver:

            numhlt = countExtn(fobj, 'HDRLET')
            new_hlt = HeaderletHDU.fromheaderlet(self)
            new_hlt.header.update('extver', numhlt + 1)
            fobj.append(new_hlt)

            wcscorr.update_wcscorr(fobj, self, 'SIPWCS', active=False)

        else:
            message = "Observation %s cannot be updated with headerlet" % (fname)
            message += " '%s'\n" % (self.hdrname)
            if not destver:
                message += " * Image %s keyword ROOTNAME not equal to " % (fname)
                message += " DESTIM = '%s'\n" % (self.destim)
            if not hdrver:
                message += " * Image %s already has headerlet " % (fname)
                message += "with HDRNAME='%s'\n" % (self.hdrname)
            logger.critical(message)

        if close_dest:
            fobj.close()

    def info(self, columns=None, pad=2, maxwidth=None,
                output=None, clobber=True, quiet=False):
        """
        Prints a summary of this headerlet
        The summary includes:
            HDRNAME  WCSNAME DISTNAME SIPNAME NPOLFILE D2IMFILE

        Parameters
        ----------
        columns: list
            List of headerlet PRIMARY header keywords to report in summary
            By default (set to None), it will use the default set of keywords
            defined as the global list DEFAULT_SUMMARY_COLS
        pad: int
            Number of padding spaces to put between printed columns
            [Default: 2]
        maxwidth: int
            Maximum column width(not counting padding) for any column in summary
            By default (set to None), each column's full width will be used
        output: string (optional)
            Name of optional output file to record summary. This filename
            can contain environment variables.
            [Default: None]
        clobber: bool
            If True, will overwrite any previous output file of same name
        quiet: bool
            If True, will NOT report info to STDOUT

        """
        summary_cols, summary_dict = self.summary(columns=columns)
        print_summary(summary_cols, summary_dict, pad=pad, maxwidth=maxwidth,
                        idcol=None, output=output, clobber=clobber, quiet=quiet)

    def summary(self, columns=None):
        """
        Returns a summary of this headerlet as a dictionary

        The summary includes a summary of the distortion model as :
            HDRNAME  WCSNAME DISTNAME SIPNAME NPOLFILE D2IMFILE

        Parameters
        ----------
        columns: list
            List of headerlet PRIMARY header keywords to report in summary
            By default(set to None), it will use the default set of keywords
            defined as the global list DEFAULT_SUMMARY_COLS

        Returns
        -------
        summary: dict
            Dictionary of values for summary
        """
        if columns is None:
            summary_cols = DEFAULT_SUMMARY_COLS
        else:
            summary_cols = columns

        # Initialize summary dict based on requested columns
        summary = {}
        for kw in summary_cols:
            summary[kw] = copy.deepcopy(COLUMN_DICT)

        # Populate the summary with headerlet values
        for kw in summary_cols:
            if kw in self[0].header:
                val = self[0].header[kw]
            else:
                val = 'INDEF'
            summary[kw]['vals'].append(val)
            summary[kw]['width'].append(max(len(val), len(kw)))

        return summary_cols, summary

    def hverify(self):
        """
        Verify the headerlet file is a valid fits file and has
        the required Primary Header keywords
        """
        self.verify()
        header = self[0].header
        assert('DESTIM' in header and header['DESTIM'].strip())
        assert('HDRNAME' in header and header['HDRNAME'].strip())
        assert('UPWCSVER' in header)

    def verify_hdrname(self, dest):
        """
        Verifies that the headerlet can be applied to the observation

        Reports whether or not this file already has a headerlet with this
        HDRNAME.
        """
        unique = verify_hdrname_is_unique(dest, self.hdrname)
        logger.debug("verify_hdrname() returned %s"%unique)
        return unique

    def verify_dest(self, dest):
        """
        verifies that the headerlet can be applied to the observation

        DESTIM in the primary header of the headerlet must match ROOTNAME
        of the science file (or the name of the destination file)
        """

        try:
            if not isinstance(dest, pyfits.HDUList):
                droot = pyfits.getval(dest, 'ROOTNAME')
            else:
                droot = dest[0].header['ROOTNAME']
        except KeyError:
            logger.debug("Keyword 'ROOTNAME' not found in destination file")
            droot = dest.split('.fits')[0]
        if droot == self.destim:
            logger.debug("verify_destim() returned True")
            return True
        else:
            logger.debug("verify_destim() returned False")
            return False

    def tofile(self, fname, destim=None, hdrname=None, clobber=False):
        """
        Write this headerlet to a file

        Parameters
        ----------
        fname: string
               file name
        destim: string (optional)
                provide a value for DESTIM keyword
        hdrname: string (optional)
                provide a value for HDRNAME keyword
        clobber: boolean
                a flag which allows to overwrte an existing file
        """
        if not destim or not hdrname:
            self.hverify()
        self.writeto(fname, clobber=clobber)

    def _del_dest_WCS(self, dest):
        """
        Delete the WCS of a science file
        """

        logger.info("Deleting all WCSs of file %s" % dest.filename())
        numext = len(dest)

        for idx in range(numext):
            # Only delete WCS from extensions which may have WCS keywords
            if ('XTENSION' in dest[idx].header and
                dest[idx].header['XTENSION'] == 'IMAGE'):
                self._remove_d2im(dest[idx])
                self._remove_sip(dest[idx])
                self._remove_lut(dest[idx])
                self._remove_primary_WCS(dest[idx])
                self._remove_idc_coeffs(dest[idx])
                self._remove_fit_values(dest[idx])
                try:
                    del dest[idx].header.ascard['VAFACTOR']
                except KeyError:
                    pass

        self._remove_ref_files(dest[0])
        self._remove_alt_WCS(dest, ext=range(numext))
        numwdvarr = countExtn(dest, 'WCSDVARR')
        numd2im = countExtn(dest, 'D2IMARR')
        for idx in range(1, numwdvarr + 1):
            del dest[('WCSDVARR', idx)]
        for idx in range(1, numd2im + 1):
            del dest[('D2IMARR', idx)]

    def _remove_ref_files(self, phdu):
        """
        phdu: Primary HDU
        """
        refkw = ['IDCTAB', 'NPOLFILE', 'D2IMFILE']
        for kw in refkw:
            try:
                del phdu.header.ascard[kw]
            except KeyError:
                pass

    def _remove_fit_values(self, ext):
        """
        Remove the any existing astrometric fit values from a FITS extension
        """

        logger.debug("Removing astrometric fit values from (%s, %s)"%
                     (ext.name, ext._extver))
        dkeys = altwcs.wcskeys(ext.header)
        if 'O' in dkeys: dkeys.remove('O') # Do not remove wcskey='O' values
        for fitkw in ['RMS_RA', 'RMS_DEC', 'NMATCH', 'CATALOG']:
            for k in dkeys:
                fkw = (fitkw+k).rstrip()
                if fkw in ext.header:
                    del ext.header[fkw]

    def _remove_sip(self, ext):
        """
        Remove the SIP distortion of a FITS extension
        """

        logger.debug("Removing SIP distortion from (%s, %s)"
                     % (ext.name, ext._extver))
        for prefix in ['A', 'B', 'AP', 'BP']:
            try:
                order = ext.header[prefix + '_ORDER']
                del ext.header[prefix + '_ORDER']
            except KeyError:
                continue
            for i in range(order + 1):
                for j in range(order + 1):
                    key = prefix + '_%d_%d' % (i, j)
                    try:
                        del ext.header[key]
                    except KeyError:
                        pass
        try:
            del ext.header['IDCTAB']
        except KeyError:
            pass

    def _remove_lut(self, ext):
        """
        Remove the Lookup Table distortion of a FITS extension
        """

        logger.debug("Removing LUT distortion from (%s, %s)"
                     % (ext.name, ext._extver))
        try:
            cpdis = ext.header['CPDIS*']
        except KeyError:
            return
        try:
            for c in range(1, len(cpdis) + 1):
                del ext.header['DP%s*...' % c]
                del ext.header[cpdis[c - 1].key]
            del ext.header['CPERR*']
            del ext.header['NPOLFILE']
            del ext.header['NPOLEXT']
        except KeyError:
            pass

    def _remove_d2im(self, ext):
        """
        Remove the Detector to Image correction of a FITS extension
        """

        logger.debug("Removing D2IM correction from (%s, %s)"
                     % (ext.name, ext._extver))
        d2imkeys = ['D2IMFILE', 'AXISCORR', 'D2IMEXT', 'D2IMERR']
        for k in d2imkeys:
            try:
                del ext.header[k]
            except KeyError:
                pass

    def _remove_alt_WCS(self, dest, ext):
        """
        Remove Alternate WCSs of a FITS extension.
        A WCS with wcskey 'O' is never deleted.
        """
        dkeys = altwcs.wcskeys(dest[('SCI', 1)].header)
        for val in ['O', '', ' ']:
            if val in dkeys:
                dkeys.remove(val) # Never delete WCS with wcskey='O'

        logger.debug("Removing alternate WCSs with keys %s from %s"
                     % (dkeys, dest.filename()))
        for k in dkeys:
            altwcs.deleteWCS(dest, ext=ext, wcskey=k)

    def _remove_primary_WCS(self, ext):
        """
        Remove the primary WCS of a FITS extension
        """

        hdr_logger.debug("Removing Primary WCS from (%s, %s)"
                     % (ext.name, ext._extver))
        naxis = ext.header.ascard['NAXIS'].value
        for key in basic_wcs:
            for i in range(1, naxis + 1):
                try:
                    del ext.header.ascard[key + str(i)]
                except KeyError:
                    pass
        try:
            del ext.header.ascard['WCSAXES']
        except KeyError:
            pass

    def _remove_idc_coeffs(self, ext):
        """
        Remove IDC coefficients of a FITS extension
        """

        logger.debug("Removing IDC coefficient from (%s, %s)"
                     % (ext.name, ext._extver))
        coeffs = ['OCX10', 'OCX11', 'OCY10', 'OCY11', 'IDCSCALE']
        for k in coeffs:
            try:
                del ext.header.ascard[k]
            except KeyError:
                pass


class HeaderletHDU(pyfits.hdu.nonstandard.FitsHDU):
    """
    A non-standard extension HDU for encapsulating Headerlets in a file.  These
    HDUs have an extension type of HDRLET and their EXTNAME is derived from the
    Headerlet's HDRNAME.

    The data itself is a FITS file embedded within the HDU data.  The file name
    is derived from the HDRNAME keyword, and should be in the form
    `<HDRNAME>_hdr.fits`.  If the COMPRESS keyword evaluates to `True`, the tar
    file is compressed with gzip compression.

    The structure of this HDU is the same as that proposed for the 'FITS'
    extension type proposed here:
    http://listmgr.cv.nrao.edu/pipermail/fitsbits/2002-April/thread.html

    The Headerlet contained in the HDU's data can be accessed by the
    `headerlet` attribute.
    """

    _extension = 'HDRLET'

    @pyfits.util.lazyproperty
    def headerlet(self):
        """Return the encapsulated headerlet as a Headerlet object.

        This is similar to the hdulist property inherited from the FitsHDU
        class, though the hdulist property returns a normal HDUList object.
        """

        return Headerlet(self.hdulist)

    @classmethod
    def fromheaderlet(cls, headerlet, compress=False):
        """
        Creates a new HeaderletHDU from a given Headerlet object.

        Parameters
        ----------
        headerlet : `Headerlet`
            A valid Headerlet object.

        compress : bool, optional
            Gzip compress the headerlet data.

        Returns
        -------
        hlet : `HeaderletHDU`
            A `HeaderletHDU` object for the given `Headerlet` that can be
            attached as an extension to an existing `HDUList`.
        """

        # TODO: Perhaps check that the given object is in fact a valid
        # Headerlet
        hlet = cls.fromhdulist(headerlet, compress)

        # Add some more headerlet-specific keywords to the header
        phdu = headerlet[0]

        if 'SIPNAME' in phdu.header:
            sipname = phdu.header['SIPNAME']
        else:
            sipname = phdu.header['WCSNAME']

        hlet.header.update('HDRNAME', phdu.header['HDRNAME'],
                           phdu.header.ascard['HDRNAME'].comment)
        hlet.header.update('DATE', phdu.header['DATE'],
                           phdu.header.ascard['DATE'].comment)
        hlet.header.update('SIPNAME', sipname, 'SIP distortion model name')
        hlet.header.update('WCSNAME', phdu.header['WCSNAME'], 'WCS name'),
        hlet.header.update('DISTNAME', phdu.header['DISTNAME'],
                           'Distortion model name'),
        hlet.header.update('NPOLFILE', phdu.header['NPOLFILE'],
                           phdu.header.ascard['NPOLFILE'].comment)
        hlet.header.update('D2IMFILE', phdu.header['D2IMFILE'],
                           phdu.header.ascard['D2IMFILE'].comment)
        hlet.header.update('EXTNAME', cls._extension, 'Extension name')

        return hlet


pyfits.register_hdu(HeaderletHDU)
