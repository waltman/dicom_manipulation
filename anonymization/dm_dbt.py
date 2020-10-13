import dicom
import os
from glob import glob

class DM_DBT:
    uncompressed_uids = set(
        ['1.2.840.10008.1.2',
         '1.2.840.10008.1.2.1',
         '1.2.840.10008.1.2.2',
        ])
    
    def __init__(self, fname, logger):
        self.fname = fname
        self.logger = logger
        self.dcm = dicom.read_file(fname)
        if 'ImagesInAcquisition' in self.dcm:
            self.num_images = int(self.dcm.ImagesInAcquisition)
        else:
            self.num_images = 1
        self.compname = None
        self.tmpfiles = []
        self.tomofiles = []

    def anon_file(self, tmpdir):
        return os.path.join(tmpdir, os.path.basename(self.fname))

    def is_mammo(self):
        if 'PresentationIntentType' not in self.dcm:
            return False
        if 'ImagesInAcquisition' not in self.dcm:
            return False
        else:
            return self.num_images == 1

    def is_tomo(self):
        if 'ImagesInAcquisition' in self.dcm and self.num_images == 1:
            return False

        if 'SeriesDescription' in self.dcm:
            desc = self.dcm.SeriesDescription
            if 'C-View' in desc:
                return True
            elif 'Tomosynthesis' in desc:
                return True
            else:
                return False
        else:
            return False

    def is_sco(self):
        return 'Tomosynthesis Reconstruction' in self.dcm.SeriesDescription

    def is_raw(self):
        if 'Series Description' in self.dcm and 'Raw' in self.dcm['Series Description']:
            return True
        elif 'PresentationIntentType' in self.dcm and self.dcm['PresentationIntentType'] == 'FOR PROCESSING':
            return True
        else:
            return False

    def is_uncompressed(self):
        return self.dcm.file_meta.TransferSyntaxUID in DM_DBT.uncompressed_uids

    def tomo_type(self):
        desc = self.dcm.SeriesDescription

        if 'C-View' in desc:
            return 'PROC_C-View'
        elif 'Raw Tomosynthesis Projection' in desc:
            return 'RAW_Tomo_PR'
        elif 'Tomosynthesis Projection' in desc:
            return 'PROC_Tomo_PR'
        elif 'Tomosynthesis Reconstruction' in desc: #SCO
            return 'PROC_Tomo_RC'
        elif 'Breast Tomosynthesis Image' in desc: #BTO
            return 'PROC_Tomo_RC'
        else:
            self.logger.debug('%s unexpected SeriesDescripion=%s' % (self.fname, self.dcm.SeriesDescription))
            return desc

    def tomo_name(self, dummy_id):
        tt = self.tomo_type()
        if tt == 'PROC_C-View':
            proc_raw = 'PROC'
            pr_rc = 'C-View'
        elif tt == 'RAW_Tomo_PR':
            proc_raw = 'RAW'
            pr_rc = 'PR'
        elif tt == 'PROC_Tomo_PR':
            proc_raw = 'PROC'
            pr_rc = 'PR'
        elif tt == 'PROC_Tomo_RC':
            proc_raw = 'PROC'
            pr_rc = 'RC'
        else:
            self.logger.debug('unexpected tomo_type of %s' % tt)
            return 'unknown'

        lat = self.dcm.Laterality
        view_pos = self.dcm.ViewPosition
        return '%s_%s_%s%s_%s' % (dummy_id, proc_raw, lat, view_pos, pr_rc)

    def decompress(self, tmpdir):
        if not self.is_tomo() or self.is_uncompressed():
            return

        # create name for output file in tmpdir
        fname, ext = os.path.splitext(os.path.basename(self.fname))
        self.compname = os.path.join(tmpdir, fname + '_decomp' + ext)

        # construct command to run
        root = os.path.dirname(__file__)
        exe = os.path.join(root, 'hologic', 'gdcmconv.exe')
        infile = os.path.join(tmpdir, fname + ext)
        command = '%s -w %s %s' % (exe, infile, self.compname)

        # run the program
        self.logger.debug('Decompressing')
        status = os.system(command)
        if status != 0:
            self.logger.warning('gdcmconv.exe failed with error %d' % status)
            self.compname = None
        else:
            self.logger.debug('OK')
            self.tmpfiles.append(self.compname)

    def expand(self, tmpdir):
        if not self.is_tomo():
            return

        if not self.is_sco():
            return
        
        # create name for output file in tmpdir
        if self.compname:
            in_fname = self.compname
        else:
            in_fname = os.path.join(tmpdir, self.fname)

        # construct command to run
        root = os.path.dirname(__file__)
        exe = os.path.join(root, 'hologic', 'gexpand.exe')
        rawflag = '-a' if self.is_raw() else ''
        prefix = 'image'
        command = '%s %s -pre %s %s %s' % (exe, rawflag, prefix, in_fname, tmpdir)

        # run the program
        self.logger.debug('Expanding')
        status = os.system(command)
        if status != 0:
            self.logger.warning('gexpand.exe failed with error %d' % status)
        else:
            self.logger.debug('OK')
            self.tomofiles = glob(os.path.join(tmpdir, 'image*.dcm'))
            self.tmpfiles += self.tomofiles

    def files(self, tmpdir):
        if self.tomofiles:
            return self.tomofiles
        elif self.compname:
            return self.compname
        else:
            return self.anon_file(tmpdir)
