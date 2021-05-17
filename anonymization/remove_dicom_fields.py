#!/usr/bin/env python
__author__ = 'HsiehM'
__EXEC__ = __file__

# Import modules here
import os, sys, csv, lockfile
from glob import glob
import traceback as tb
import logging as log
from pandas import read_csv
from tempfile import mkdtemp
from shutil import rmtree, copy2, move

import id_linking as il
from dm_dbt import DM_DBT

## Create a logger
try:  # Python 2.7+
    from logging import NullHandler
except ImportError:
    class NullHandler(log.Handler):
        def emit(self, record):
            pass

def _int_from_hex(i):
    return int(i, 16)
    
log.getLogger().addHandler(NullHandler())

logger = log.getLogger(__name__)
ch = log.StreamHandler()
formatter = log.Formatter(fmt = '%(asctime)s %(name)s %(levelname)s: %(message)s',
                          datefmt = '%Y%m%d-%H:%M:%S')
ch.setFormatter(formatter)
logger.addHandler(ch)

## load default tags to anonymize
## Basic Application Level Confidentiality Profile Attributes
## Annex E, E1 ftp://medical.nema.org/medical/dicom/2008/08_15pu.pdf

_source_dir, _ = os.path.split(__file__)
_dicom_tag_file = os.path.join(_source_dir, 'dicom_anon_tags.csv')
_df = read_csv(_dicom_tag_file, converters = {'Tag': _int_from_hex}, engine='python', skipfooter = 2)

_fields_subject_to_anonymize=[]
_fields_subject_to_anonymize_string=[]
for c in ['Remove', 'Replace', 'ReplaceDate']:
    _condition = 'AnonymizedByCBIG in "%s"' % c
    _fields = _df.query(_condition)['Tag'].values
    _fields_in_string = [hex(i).strip('L') for i in _fields]
    _fields_subject_to_anonymize.append(_fields)
    _fields_subject_to_anonymize_string.append(_fields_in_string)

_fields_to_remove, _fields_to_replace, _fields_to_replace_date = _fields_subject_to_anonymize

_fields_to_remove_string, _fields_to_replace_string, _fields_to_replace_date_string = _fields_subject_to_anonymize_string

## Read CBIG shift pattern
_pattern_file = _dicom_tag_file = os.path.join(_source_dir, 'sample_anonpattern.cfg')
with open(_pattern_file, 'r') as _fid:
    _shift_pattern = _fid.readline().strip('\n')
    _date_shift_pattern = _fid.readline()
    
def discover_files(input_dir, recursive = False):
    ''' Return a list of files found in a given directory.
    
        :param input_dir: An input directory to discover dicom files.
        :type input_dir: str
        :param recursive: A switch to perform the operation recursively. It might be time-consuming.
        :type recursive: boolean
        :returns: A list of files found in input_dir.
    '''
    input_dir = os.path.abspath(input_dir)
    if recursive:
        src_files = []
        for dirpath, dirnames, filenames in os.walk(input_dir):
            # ignore hidden files and directories
            filenames = [f for f in filenames if not f[0] == '.']
            dirnames[:] = [d for d in dirnames if not d[0] == '.']
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                src_files.append(filepath)
    else:
        src_files = glob(os.path.join(input_dir, '*'))

    return src_files

def anonymize_input(fname, fields_to_remove, fields_to_replace = None, dates_to_replace = None, study_id = None, odir = None):

    logger.debug('Anonymizing %s' % fname)

    csvout = os.path.join(odir, 'idLookup.csv')

    head, tail = os.path.split(fname)
    _, dir_id = os.path.split(head)

    logger.debug('loading dicom...')
    dm_dbt = DM_DBT(fname, logger)
    logger.debug('done loading dicom.')
    if not dm_dbt.is_tomo():
        logger.debug('%s is not tomo, skipping' % fname)
        return 0
    
    ds = dm_dbt.dcm
    
    if ds[0x0008, 0x0050].value.isdigit():
        dummy_id = il.get_fake_ID(ds[0x0008, 0x0050].value, _shift_pattern)
        logger.debug('%s -> %s' % (ds[0x0008, 0x0050].value, dummy_id))
    else:
        logger.warning('Accession Number is not numeric thus shifting is not supported. Use directory name instead.')
        dummy_id = il.get_fake_ID(dir_id, _shift_pattern)
        logger.debug('%s (%s) -> %s' % (ds[0x0008, 0x0050].value, dir_id, dummy_id))

    odir = os.path.join(odir, dummy_id)
    try:
        os.makedirs(odir)
    except:
        pass
    
    header = ['AccessionNumber', 'InputDir', 'DummyID']
    write_to_csv(csvout, [ds[0x0008, 0x0050].value, dir_id, dummy_id], header, fname)

    tmpdir = mkdtemp()
    anonymize_fields(dm_dbt, fields_to_remove, fields_to_replace, dates_to_replace, study_id, tmpdir)
    dm_dbt.decompress(tmpdir)
    dm_dbt.expand(tmpdir)

    logger.debug('Copying images')
    copy_images(dummy_id, tmpdir, dm_dbt, odir)

    # cleanup tmpdir
    logger.debug('Removing temp files')
    rmtree(tmpdir)

    return 0

def write_to_csv(fname, array, header, subject):
    head, tail = os.path.split(fname)
    if not os.path.isdir(head):
        os.mkdir(head)

    array = list(array)
    array.insert(0, subject)
    header = list(header)
    header.insert(0, 'Image')

    # file locking mechanism. A process/thread would only write to fname only if it can acquire the lock.
    lock = lockfile.FileLock(fname)
    lock.timeout = 200
    try:
        with lock:
            isfile = os.path.isfile(fname)
            with open(fname, 'a+') as f:
                writer = csv.writer(f, delimiter = ',')
                if not isfile:
                    writer.writerow(header)
                writer.writerow(array)
    except lockfile.LockTimeout:
        # lock.unique_name: hostname-tname.pid-somedigits
        unique_name = os.path.split(lock.unique_name)[1]
        fname_tmp = fname + '_' + unique_name
        logger.warning('Lock timeout. Log the entry to ' + fname_tmp)
        with open(fname_tmp, 'a+') as f:
            writer = csv.writer(f, delimiter = ',')
            writer.writerow(header)
            writer.writerow(array)

def anonymize_fields(dm_dbt, fields_to_remove, fields_to_replace, dates_to_replace, study_id, odir):

    logger.debug('Anonymizing fields in %s' % dm_dbt.fname)

    ds = dm_dbt.dcm
    _, tail = os.path.split(dm_dbt.fname)
    dm_dbt.anonname = os.path.join(odir, tail)

    for tag in fields_to_replace:
        if tag in ds:
            logger.debug('Tag to replace: %s %s' % (ds[tag].tag, ds[tag].name))
            if ds[tag].value.isdigit():
                dummy_id = il.get_fake_ID(ds[tag].value, _shift_pattern)
                ds[tag].value = dummy_id #'Anonymized'
            else:
                logger.warning('Tag value of %s %s is not numeric thus shifting is not supported. Removing tag value instead.' % (ds[tag].tag, ds[tag].name))
                ds[tag].value = ''

    for tag in fields_to_remove:
        if tag in ds:
            logger.debug('Tag to remove: %s %s' % (ds[tag].tag, ds[tag].name))
            ds[tag].value = '' #'Anonymized'

    for tag in dates_to_replace:
        if tag in ds:
            dummy_date = il.get_fake_ID(ds[tag].value, _date_shift_pattern)
            ds[tag].value = dummy_date #'Anonymized'

    if study_id is not None and (0x0020, 0x0010) in ds:
        ds[0x0020, 0x0010].value = study_id

    ds.save_as(dm_dbt.anonname)
    logger.debug('Anonymized %s' % dm_dbt.anonname)
    return 0

def copy_images(dummy_id, tmpdir, dm_dbt, odir):
    # make standard subdirectories if they don't already exist
    subdirs = ['PROC_C-View',
               'PROC_Tomo_PR',
               'PROC_Tomo_RC',
               'RAW_Tomo_PR',
              ]
    for subdir in subdirs:
        dirpath = os.path.join(odir, subdir)
        if not os.path.isdir(dirpath):
            os.mkdir(dirpath)

    tomo_type = dm_dbt.tomo_type()
    tomo_name = dm_dbt.tomo_name(dummy_id)
    subdir = os.path.join(odir, tomo_type, tomo_name)
    subdir = handle_collisions(subdir)
    os.mkdir(subdir)

    fnames = dm_dbt.files(tmpdir)
    if type(fnames) is str:
        dst_fname = tomo_name + '.dcm'
        dst = os.path.join(subdir, dst_fname)
        command = 'copy /B /Y "%s" "%s"' % (fnames, dst)
        logger.debug(command)
        status = os.system(command)
        if status != 0:
            logger.warning('Copy failed with error %d' % status)
    else:
        for fname in fnames:
            dst = os.path.join(subdir, os.path.basename(fname))
            command = 'copy /B /Y "%s" "%s"' % (fname, dst)
            logger.debug(command)
            status = os.system(command)
            if status != 0:
                logger.warning('Copy failed with error %d' % status)

def handle_collisions(subdir):
    dirnames = glob(subdir + '*')
    if len(dirnames) == 0: # no collisions, so keep name
        return subdir
    else:
        if len(dirnames) == 1 and subdir in dirnames: # at _1 to the end of it
            move(subdir, subdir + "_1")

        # now number subdir so it's the number of dirnames in the glob, + 1
        return '%s_%d' % (subdir, len(dirnames) + 1)

def create_parser():
    import argparse
    ''' Create an argparse.ArgumentParser object

        :returns: An argparse.ArgumentParser parser.
    '''
    parser = argparse.ArgumentParser(prog = __EXEC__,
                                     description = 'Anonymize DICOM files according to "Basic Application Level Confidentiality Profile Attributes" by default. User can specify what fields to strip off the patient identifiable information too.')
    # Required
    parser.add_argument('-i', '--input',
                        required = True,
                        dest = 'idir',
                        action = 'store',
                        type = str,
                        help = 'Input directory to find files')

    # Optional
    parser.add_argument('-o', '--output',
                        dest = 'odir',
                        action = 'store',
                        type = str,
                        help = 'Output directory to find files. If not specified, it will overwrite the input files directly.')
    parser.add_argument('-f', '--fields',
                        dest = 'fields',
                        action = 'store',
                        default = _fields_to_remove,
                        type = str,
                        nargs='+',
                        help = 'Fields to remove. Default: Basic Application Level Confidentiality Profile Attributes %s' % _fields_to_remove_string)
    parser.add_argument('-s', '--study_id',
                        dest = 'study_id',
                        action = 'store',
                        type = str,
                        help = 'Study ID to replace string in (0x200010).')
    parser.add_argument('-r', '--recursive',
                        dest = 'recursive',
                        action = 'store_true',
                        default = False,
                        help = 'Find dicoms recursively. Default: False')
    parser.add_argument('-v', '--verbose',
                        dest = 'verbosity',
                        action = 'count',
                        help = 'Increase verbosity of the program. By calling the flag multiple time, the verbosity can be further increased. Max: 2 levels (-v -v)')
    parser.add_argument('-l', '--logfile',
                        dest = 'logfile',
                        action = 'store',
                        type = str,
                        help = 'Filename to save log messages. If not specified, log messages will only go to stderr.')
                        
    return parser
    
    
def main(argv = None):
    if argv is None:
        argv = sys.argv[1:]
    # parse input from command line
    parser = create_parser()
    args = parser.parse_args(argv)
    
    import socket, time

    exe_folder = os.getcwd()
    exe_time = time.strftime("%Y-%m-%d %a %H:%M:%S", time.localtime())
    host = socket.gethostname()
    print "Command", __EXEC__
    print "Arguments", args
    print "Executing on", host
    print "Executing at", exe_time
    print "Executing in", exe_folder

    # Start of the program
    log_level = log.WARNING
    if args.verbosity == 2:
        log_level = log.DEBUG
    elif args.verbosity == 1:
        log_level = log.INFO
             
    logger.setLevel(log_level)
    
    if args.logfile:
        chfh = log.FileHandler(args.logfile)
        formatter2 = log.Formatter(fmt = '%(asctime)s %(name)s %(levelname)s: %(message)s',
                                  datefmt = '%Y%m%d-%H:%M:%S')
        chfh.setFormatter(formatter2)
        logger.addHandler(chfh)

    #logger.info('Fields anonymizing: %s' % args.fields)
    dcms = discover_files(args.idir, recursive = args.recursive)
    logger.info('Anonymizing %d dicoms in %s' % (len(dcms), args.idir)) 
    
    # TODO 20180607 odir setup needs more consideration for various of situation.
    if args.odir is None:
        if args.study_id is None:
            odir = args.idir
        else:
            odir = os.path.join(args.idir, args.study_id)
    else:
        if args.study_id is not None:
            odir = os.path.join(args.odir, args.study_id)
        else:
            odir = args.odir
    
    try:
        os.makedirs(odir)
    except:
        pass
    
    status_codes = []    
    for f in dcms:
        try:
            status=anonymize_input(f, args.fields, fields_to_replace = _fields_to_replace, dates_to_replace = _fields_to_replace_date, study_id = args.study_id, odir = odir)
        except:
            logger.error('Failed to anonymize %s' % f)
            tb.print_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
            status=1
        status_codes.append(status)
    
    return int(any(status_codes))

if __name__ == '__main__':
    sys.exit(main())

