__version__ = "0.1.1"

import sys, os
import time
import math
import traceback

import numpy
from scipy.stats import beta

from collections import defaultdict, namedtuple
from itertools import chain, izip
from bisect import bisect
from copy import copy

import multiprocessing
import Queue

from igraph import Graph

from files.reads import RNAseqReads, CAGEReads, RAMPAGEReads, PolyAReads, \
    clean_chr_name, fix_chrm_name_for_ucsc, \
    guess_strand_from_fname, iter_coverage_intervals_for_read
from files.junctions import extract_junctions_in_region, \
    extract_junctions_in_contig
from files.bed import create_bed_line
from files.gtf import parse_gtf_line, load_gtf

from lib.logging import Logger
# log statement is set in the main init, and is a global
# function which facilitates smart, ncurses based logging
log_statement = None


NTHREADS = 1
MAX_THREADS_PER_CONTIG = 16
TOTAL_MAPPED_READS = None
MIN_INTRON_SIZE = 40

# the maximum number of bases to expand gene boundaries from annotated genes
MAX_GENE_EXPANSION = 1000

class ThreadSafeFile( file ):
    def __init__( self, *args ):
        args = list( args )
        args.insert( 0, self )
        file.__init__( *args )
        self.lock = multiprocessing.Lock()

    def write( self, line ):
        with self.lock:
            file.write( self, line )
            self.flush()

def flatten( regions ):
    new_regions = []
    curr_start = regions[0][0]
    curr_end = regions[0][1]
    for i,(start,end) in enumerate(regions):
        if curr_end > end:
            end = curr_end
        if i+1 == len( regions ): 
            if len(new_regions) == 0:
                new_regions = [curr_start, curr_end]
                break
            if new_regions[-1][1] == end:
                break
            else:
                new_regions.append( [ curr_start, curr_end] )
                break
        if regions[i+1][0]-end <= 1:
            curr_end = max( regions[i+1][1], end ) 
        else:
            new_regions.append( [curr_start, curr_end] )
            curr_start = regions[i+1][0]
            curr_end = regions[i+1][1]
    if type(new_regions[0]) == int:
        return [new_regions]
    else:
        return new_regions

MIN_REGION_LEN = 50
MIN_EMPTY_REGION_LEN = 100
MIN_EXON_BPKM = 0.01
EXON_EXT_CVG_RATIO_THRESH = 5
POLYA_MERGE_SIZE = 100

CAGE_PEAK_WIN_SIZE = 30
MIN_NUM_CAGE_TAGS = 5
MAX_CAGE_FRAC = 0.05
NUM_TSS_BASES_TO_SKIP = 200
NUM_TES_BASES_TO_SKIP = 300

def get_contigs_and_lens( reads_files ):
    """Get contigs and their lengths from a set of bam files.
    
    We make sure that the contig lengths are consistent in all of the bam files, and
    we remove contigs that dont have at least 1 read in at least one rnaseq file 
    and one promoter reads file.
    """
    chrm_lengths = {}
    contigs = None
    for bam in reads_files:
        bam_contigs = set()
        for ref_name, ref_len in zip(bam.references, bam.lengths):
            # add the contig to the chrm lengths file, checking to
            # make sure that lengths match if it has already been added
            if clean_chr_name( ref_name ) not in chrm_lengths:
                chrm_lengths[clean_chr_name( ref_name )] = ref_len
            else:
                assert chrm_lengths[clean_chr_name(ref_name)] == ref_len, \
                    "Chromosome lengths do not match between bam files"
            bam_contigs.add( clean_chr_name(ref_name) )
        
        if contigs == None:
            contigs = bam_contigs
        else:
            contigs = contigs.intersection( bam_contigs )
    
    # remove contigs that dont have reads in at least one file
    def at_least_one_bam_has_reads( chrm, bams ):
        for bam in reads_files:
            try:
                next( bam.fetch( chrm ) )
            except StopIteration:
                continue
            except KeyError:
                continue
            else:
                return True
    
    # produce the final list of contigs
    rv =  {}
    for key, val in chrm_lengths.iteritems():
        if key in contigs and any( 
            at_least_one_bam_has_reads(key, reads) for reads in reads_files ):
            rv[key] = val
    
    return rv

def build_empty_array():
    return numpy.array(())

def find_empty_regions( cov, thresh=1 ):
    return []
    x = numpy.diff( numpy.asarray( cov >= thresh, dtype=int ) )
    return zip(numpy.nonzero(x==1)[0],numpy.nonzero(x==-1)[0])

def merge_empty_labels( poss ):
    locs = [i[1] for i in poss ]
    labels_to_remove = []
    label_i = 0
    while label_i < len(locs):
        if locs[label_i] != "ESTART": 
            label_i += 1
            continue
        label_n = label_i+1
        while label_n < len(locs) \
                and locs[ label_n ] in ("ESTART", "ESTOP"):
            label_n += 1
        if label_n - label_i > 1:
            labels_to_remove.append( [label_i+1, label_n-1]  )
        label_i = label_n+1
    
    for start, stop in reversed( labels_to_remove ):
        del poss[start:stop+1]

    return poss

def get_qrange_long( np, w_step, w_window ):
    L = len(np)-w_window
    if L < 2:
       nm, nx = get_qrange_short( np )
       return nm, nx
    Q = []
    for pos in xrange(0, L, w_step):
        Q.append( np[pos:pos+w_window].mean() )
    Q = numpy.asarray(Q)
    Q.sort()
    return Q[int(len(Q)*0.1)], Q[int(len(Q)*0.9)]

def get_qrange_short( np ):
    L = len(np)
    return np.min(), np.max()

def filter_exon(exon, wig, num_start_bases_to_skip=0, num_stop_bases_to_skip=0):
    '''Find all the exons that are sufficiently homogenous and expressed.
    
    '''
    start = exon.start + num_start_bases_to_skip
    end = exon.stop - num_stop_bases_to_skip
    if end - start < MIN_INTRON_SIZE: return False
    vals = wig[start:end+1]
    n_div = max( 1, int(len(vals)/MIN_INTRON_SIZE) )
    div_len = len(vals)/n_div
    for i in xrange(n_div):
        seg = vals[i*div_len:(i+1)*div_len]
        if seg.mean() < MIN_EXON_BPKM:
            return True

    return False

def filter_exons( exons, rnaseq_cov, 
                  num_start_bases_to_skip=0, 
                  num_stop_bases_to_skip=0 ):
    for exon in exons:
        if not filter_exon( exon, rnaseq_cov, 
                            num_start_bases_to_skip, 
                            num_stop_bases_to_skip ):
            yield exon
    
    return

class Bin( object ):
    def __init__( self, start, stop, left_label, right_label, 
                  bin_type=None, score=1000 ):
        self.start = start
        self.stop = stop
        assert stop - start > 0
        self.left_label = left_label
        self.right_label = right_label
        self.type = bin_type
        self.score = score
    
    def length( self ):
        return self.stop - self.start + 1
    
    def mean_cov( self, cov_array ):
        return numpy.median(cov_array[self.start:self.stop])
        return cov_array[self.start:self.stop].mean()
    
    def reverse_strand(self, contig_len):
        return Bin(contig_len-self.stop, contig_len-self.start, 
                   self.right_label, self.left_label, self.type)

    def reverse_coords(self, contig_len):
        return Bin(contig_len-self.stop, contig_len-self.start, 
                   self.left_label, self.right_label, self.type)
    
    def shift(self, shift_amnt):
        return Bin(self.start+shift_amnt, self.stop+shift_amnt, 
                   self.left_label, self.right_label, self.type)
    
    def __repr__( self ):
        if self.type == None:
            return "%i-%i:%s:%s" % ( self.start, self.stop, self.left_label,
                                       self.right_label )

        return "%s:%i-%i" % ( self.type, self.start, self.stop )
    
    def __hash__( self ):
        return hash( (self.start, self.stop, self.type, 
                      self.left_label, self.right_label) )
    
    def __eq__( self, other ):
        return ( self.start == other.start and self.stop == other.stop )
    
    _bndry_color_mapping = {
        'CONTIG_BNDRY': '0,0,0',
        'GENE_BNDRY': '0,0,0',
        
        'POLYA': '255,255,0',

        'CAGE_PEAK': '0,255,0',
        
        'D_JN': '173,255,47',
        'R_JN': '0,0,255',
        
        'ESTART': '0,0,0',
        'ESTOP': '0,0,0'
    }
    
    def find_bndry_color( self, bndry ):
        return self._bndry_color_mapping[ bndry ]
    
    def _find_colors( self, strand ):
        if self.type != None:
            if self.type =='GENE':
                return '0,0,0'
            if self.type =='CAGE_PEAK':
                return '0,0,0'
            if self.type =='EXON':
                return '0,0,0'
            if self.type =='EXON_EXT':
                return '0,0,255'
            if self.type =='RETAINED_INTRON':
                return '255,255,0'
            if self.type =='TES_EXON':
                return '255,0,0'
            if self.type =='TSS_EXON':
                return '0,255,0'
            if self.type =='SE_GENE':
                return '255,255,0'

        if strand == '+':
            left_label, right_label = self.left_label, self.right_label
        else:
            assert strand == '-'
            left_label, right_label = self.right_label, self.left_label
        
        
        if left_label == 'D_JN' and right_label  == 'R_JN':
            return '108,108,108'
        if left_label == 'D_JN' and right_label  == 'D_JN':
            return '135,206,250'
        if left_label == 'R_JN' and right_label  == 'R_JN':
            return '135,206,250'
        if left_label == 'R_JN' and right_label  == 'D_JN':
            return '0,0,255'
        if left_label == 'R_JN' and right_label  == 'POLYA':
            return '255,0,0'
        if left_label == 'POLYA' and right_label  == 'POLYA':
            return ' 240,128,128'
        if left_label == 'D_JN' and right_label  == 'POLYA':
            return '240,128,128'
        if left_label == 'POLYA' and right_label  == 'D_JN':
            return '147,112,219'
        if left_label == 'POLYA' and right_label  == 'R_JN':
            return '159,153,87'
        if left_label == 'ESTART' and right_label  == 'ESTOP':
            return '159,153,87'
        
        return ( self.find_bndry_color(left_label), 
                 self.find_bndry_color(right_label) )

def write_unified_bed( elements, ofp ):
    assert isinstance( elements, Bins )
    
    feature_mapping = { 
        'GENE': 'gene',
        'CAGE_PEAK': 'promoter',
        'SE_GENE': 'single_exon_gene',
        'TSS_EXON': 'tss_exon',
        'EXON': 'internal_exon',
        'TES_EXON': 'tes_exon',
        'INTRON': 'intron',
        'POLYA': 'polya'
    }

    color_mapping = { 
        'GENE': '200,200,200',
        'CAGE_PEAK': '153,255,000',
        'SE_GENE': '000,000,200',
        'TSS_EXON': '140,195,59',
        'EXON': '000,000,000',
        'TES_EXON': '255,51,255',
        'INTRON': '100,100,100',
        'POLYA': '255,0,0'
    }
        
    for bin in elements:
        chrm = elements.chrm
        if FIX_CHRM_NAMES_FOR_UCSC:
            chrm = fix_chrm_name_for_ucsc(chrm)
        region = ( chrm, elements.strand, bin.start, bin.stop)
        grp_id = feature_mapping[bin.type] + "_%s_%s_%i_%i" % region
        
        # subtract 1 because we work in 1 based coords, but beds are 0-based
        # also, add 1 to stop because beds are open-closed ( which means no net 
        # change for the stop coordinate )
        bed_line = create_bed_line( chrm, elements.strand, 
                                    bin.start-1, bin.stop, 
                                    feature_mapping[bin.type],
                                    score=bin.score,
                                    color=color_mapping[bin.type],
                                    use_thick_lines=(bin.type != 'INTRON'))
        ofp.write( bed_line + "\n"  )
    return

class Bins( list ):
    def __init__( self, chrm, strand, iter=[] ):
        self.chrm = chrm
        self.strand = strand
        self.extend( iter )
        if FIX_CHRM_NAMES_FOR_UCSC:
            chrm = fix_chrm_name_for_ucsc(chrm)
        
        self._bed_template = "\t".join( [chrm, '{start}', '{stop}', '{name}', 
                                         '1000', strand, '{start}', '{stop}', 
                                         '{color}']  ) + "\n"
        
    def reverse_strand( self, contig_len ):
        rev_bins = Bins( self.chrm, self.strand )
        for bin in reversed(self):
            rev_bins.append( bin.reverse_strand( contig_len ) )
        return rev_bins

    def reverse_coords( self, contig_len ):
        rev_bins = Bins( self.chrm, self.strand )
        for bin in reversed(self):
            rev_bins.append( bin.reverse_coords( contig_len ) )
        return rev_bins

    def shift(self, shift_amnt ):
        shifted_bins = Bins( self.chrm, self.strand )
        for bin in self:
            shifted_bins.append( bin.shift( shift_amnt ) )
        return shifted_bins
    
    def writeBed( self, ofp ):
        """
            chr7    127471196  127472363  Pos1  0  +  127471196  127472363  255,0,0
        """
        for bin in self:
            length = max( (bin.stop - bin.start)/4, 1)
            colors = bin._find_colors( self.strand )
            if isinstance( colors, str ):
                op = self._bed_template.format(
                    start=bin.start,stop=bin.stop+1,color=colors, 
                    name="%s_%s"%(bin.left_label, bin.right_label) )
                ofp.write( op )
            else:
                op = self._bed_template.format(
                    start=bin.start,stop=(bin.start + length),color=colors[0], 
                    name=bin.left_label)
                ofp.write( op )
                
                op = self._bed_template.format(
                    start=(bin.stop-length),stop=bin.stop,color=colors[1],
                    name=bin.right_label)
                ofp.write( op )
        
        return

    def writeGff( self, ofp ):
        """
            chr7    127471196  127472363  Pos1  0  +  127471196  127472363  255,0,0
        """
        if self.strand == '-':
            writetable_bins = self.reverse_strand( contig_len )
        else:
            writetable_bins = self
        
        for bin in writetable_bins:
            if filter != None and bin.type != filter:
                continue
            chrm = elements.chrm
            if FIX_CHRM_NAMES_FOR_UCSC:
                chrm = fix_chrm_name_for_ucsc(self.chrm)
            region = GenomicInterval(chrm, self.strand, 
                                     bin.start, bin.stop)
            grp_id = "%s_%s_%i_%i" % region
            ofp.write( create_gff_line(region, grp_id) + "\n" )
        
        return

def load_junctions_worker(all_jns, all_jns_lock, args):
    log_statement( "Finding jns in '%s:%s:%i-%i'" % args[1:5] )
    jns = extract_junctions_in_region( *args )
    with all_jns_lock:
        all_jns.extend( jns )
    del jns
    log_statement( "" )
    return

def load_junctions_in_bam( reads, (chrm, strand, contig_len) ):
    if NTHREADS == 1:
        return extract_junctions_in_contig( reads, chrm, strand )
    else:
        nthreads = min( NTHREADS, MAX_THREADS_PER_CONTIG )
        seg_len = int((contig_len)/nthreads)
        segments =  [ [i*seg_len, (i+1)*seg_len] for i in xrange(nthreads) ]
        segments[0][0] = 0
        segments[-1][1] = contig_len

        from multiprocessing import Process, Manager
        manager = Manager()
        all_jns = manager.list()
        all_jns_lock = multiprocessing.Lock()

        ps = []
        for start, stop in segments:
            p = Process(target=load_junctions_worker,
                        args=( all_jns, all_jns_lock, 
                               (reads, chrm, strand, start, stop, True) 
                               ) )

            p.start()
            ps.append( p )

        log_statement( "Waiting on jn finding children in contig '%s' on '%s' strand" % ( chrm, strand ) )
        while True:
            if all( not p.is_alive() for p in ps ):
                break
            time.sleep( 0.1 )

        junctions = defaultdict( int )
        for jn, cnt in all_jns:
            junctions[jn] += cnt

        return sorted(junctions.iteritems())
    assert False
    
def load_junctions( rnaseq_reads, cage_reads, polya_reads, 
                    (chrm, strand, contig_len) ):
    # load and filter the ranseq reads. We can't filter all of the reads because
    # they are on differnet scales, so we only filter the RNAseq and use the 
    # cage and polya to get connectivity at the boundaries.
    rnaseq_junctions = load_junctions_in_bam(
        rnaseq_reads, (chrm, strand, contig_len))
    
    # filter junctions
    jn_starts = defaultdict( int )
    jn_stops = defaultdict( int )
    for (start, stop), cnt in rnaseq_junctions:
        jn_starts[start] = max( jn_starts[start], cnt )
        jn_stops[stop] = max( jn_stops[stop], cnt )
    
    filtered_junctions = defaultdict(int)
    for (start, stop), cnt in rnaseq_junctions:
        if (float(cnt)+1)/jn_starts[start] < 0.01: continue
        if (float(cnt)+1)/jn_stops[stop] < 0.01: continue
        if stop - start > 10000000: continue
        filtered_junctions[(start, stop)] = cnt
    
    # add in the cage and polya reads, for connectivity
    for reads in [cage_reads, polya_reads]:
        if reads == None: continue
        for jn, cnt in load_junctions_in_bam(reads, (chrm,strand,contig_len)):
            filtered_junctions[jn] += 0
    return filtered_junctions

def find_initial_segmentation_worker( 
        candidate_boundaries, candidate_boundaries_lock,
        accepted_boundaries, accepted_boundaries_lock,
        chrm, strand, 
        rnaseq_reads, cage_reads, polya_reads ):
    log_statement( "Finding Segments (%s:%s)" % ( chrm, strand ))
    def no_signal( start, stop ):
        try: next( rnaseq_reads.iter_reads(chrm, strand, start, stop ) )
        except StopIteration: pass
        else: return False
        
        if cage_reads != None:
            try: next( cage_reads.iter_reads(chrm, strand, start, stop ) )
            except StopIteration: pass
            else: return False
        
        if polya_reads != None:
            try: next( polya_reads.iter_reads(chrm, strand, start, stop ) )
            except StopIteration: pass
            else: return False
        
        return True

    locs = []
    while True:
        with candidate_boundaries_lock:
            try:
                start, stop = candidate_boundaries.pop()
            except IndexError:
                break
        if not no_signal( start, stop ):
            locs.append( (start, stop) )
    
    log_statement( "Putting Segments in Queue (%s, %s)" % (chrm, strand) )
    with accepted_boundaries_lock:
        accepted_boundaries.append( locs )
    log_statement( "" )
    return

def find_gene_boundaries((chrm, strand, contig_len), 
                         rnaseq_reads, 
                         cage_reads,
                         polya_reads,
                         junctions=None):
    
    def find_segments_with_signal( chrm, strand, rnaseq_reads ):
        # initialize a tiling of segments acfross the genome to check for signal
        # This is expensive, so we farm it out to worker processes. But, 
        # the simple algorithm would be to 
        manager = multiprocessing.Manager()
        candidate_segments = manager.list()
        candidate_segments_lock = manager.Lock()
        for middle in xrange( MIN_INTRON_SIZE/2, 
                              contig_len-MIN_INTRON_SIZE/2, 
                              MIN_INTRON_SIZE ):
            
            candidate_segments.append( 
                (middle-MIN_INTRON_SIZE/2, middle+MIN_INTRON_SIZE/2) )
        accepted_segments = manager.list()
        accepted_segments_lock = manager.Lock()
        
        ps = []
        for i in xrange(MAX_THREADS_PER_CONTIG):
            args = (candidate_segments, candidate_segments_lock,
                    accepted_segments, accepted_segments_lock,
                    chrm, strand, rnaseq_reads, cage_reads, polya_reads )
            p = multiprocessing.Process( 
                target=find_initial_segmentation_worker, args=args)
            p.start()
            ps.append( p )
        
        n_bndries = len(candidate_segments)
        while True:
            log_statement(
                "Waiting on segmentation children in %s:%s (%i/%i remain)" 
                % (chrm, strand, len(candidate_segments), n_bndries),
                do_log=False )
            
            if all( not p.is_alive() for p in ps ):
                break
            time.sleep( 0.5 )

        log_statement( "Merging segments queue in %s:%s" 
                       % ( chrm, strand ) )        

        locs = []
        with accepted_segments_lock:
            for bndries in accepted_segments:
                locs.extend( bndries )
        
        # merge adjoining segments
        locs.sort()
        new_locs = [locs.pop(0),]
        for start, stop in locs:
            if start == new_locs[-1][1]:
                new_locs[-1] = (new_locs[-1][0], stop)
            else:
                new_locs.append( (start, stop) )
        
        log_statement( "Finished segmentation in %s:%s" 
                       % ( chrm, strand ) )
        return new_locs
    
    def merge_polya_segments( segments, strand, window_len = 10 ):
        bndries = numpy.array( sorted(segments, reverse=(strand!='-')) )
        bndries_to_delete = set()
        for i, bndry in enumerate(bndries[1:-1]):
            if bndry - bndries[i+1-1] > 1000000:
                continue
            pre_bndry_cnt = 0
            post_bndry_cnt = 0
            
            cvg = rnaseq_reads.build_read_coverage_array( 
                chrm, strand, max(0,bndry-window_len), bndry+window_len )
            pre_bndry_cnt += cvg[:window_len].sum()
            post_bndry_cnt += cvg[window_len:].sum()
            
            if strand == '-':
                pre_bndry_cnt, post_bndry_cnt = post_bndry_cnt, pre_bndry_cnt
            if (post_bndry_cnt)/(post_bndry_cnt+pre_bndry_cnt+1e-6) > 0.20:
                bndries_to_delete.add( bndry )
        
        for bndry_to_delete in bndries_to_delete:
            del segments[bndry_to_delete]
        
        return segments
    
    def cluster_segments( segments, jns ):
        boundaries = numpy.array(sorted(chain(*segments)))
        edges = set()
        for (start, stop), cnt in jns:
            start_bin = boundaries.searchsorted( start-1 )-1
            stop_bin = boundaries.searchsorted( stop+1 )-1
            if start_bin != stop_bin:
                edges.add((int(min(start_bin, stop_bin)), 
                           int(max(start_bin, stop_bin))))
        
        genes_graph = Graph( len( boundaries )-1 )
        genes_graph.add_edges( list( edges ) )
        
        segments = []
        for g in genes_graph.clusters():
            segments.append( (boundaries[min(g)]+1, boundaries[max(g)+1]-1) )
        
        return flatten( segments )
    
    # find all of the junctions
    if None == junctions:
        junctions = load_junctions(rnaseq_reads, cage_reads, polya_reads, 
                                   (chrm, strand, contig_len))
    
    # find segment boundaries
    if VERBOSE: log_statement( 
        "Finding segments for %s:%s" % (chrm, strand) )
    segments = find_segments_with_signal(chrm, strand, rnaseq_reads)
    
    #if VERBOSE: log_statement( "Merging segments for %s %s" % (chrm, strand) )
    #merged_segments = merge_segments( initial_segmentation, strand )
    # because the segments are disjoint, they are implicitly merged
    merged_segments = segments
    
    if VERBOSE: log_statement( "Clustering segments for %s %s" % (chrm, strand))
    clustered_segments = cluster_segments( merged_segments, junctions )
    
    # build the gene bins, and write them out to the elements file
    genes = Bins( chrm, strand, [] )
    if len( clustered_segments ) == 2:
        return genes
    
    for start, stop in clustered_segments:
        if stop - start < 300: continue
        genes.append( Bin(max(1,start-10), min(stop+10,contig_len), 
                          "ESTART", "ESTOP", "GENE" ) )
    
    return genes

def filter_polya_peaks( polya_peaks, rnaseq_cov, jns ):
    if len(polya_peaks) == 0:
        return polya_peaks
    
    polya_peaks.sort()

    new_polya_peaks = []
    for start, stop in polya_peaks[:-1]:
        pre_cvg = rnaseq_cov[max(0,start-10):start].sum()
        # find the contribution of jn 
        jn_cnt = sum( 1 for jn_start, jn_stop, jn_cnt in jns 
                      if abs(stop - jn_start) <= 10+stop-start )
        if jn_cnt > 0:
            continue
        pre_cvg -= 100*jn_cnt
        post_cvg = rnaseq_cov[stop+10:stop+20].sum()
        if pre_cvg > 10 and pre_cvg/(post_cvg+1.0) < 5:
            continue
        else:
            new_polya_peaks.append( [start, stop] )
    
    new_polya_peaks.append( polya_peaks[-1] )
    polya_peaks = new_polya_peaks

    # merge sites that are close
    new_polya_peaks = [polya_peaks[0],]
    for start, stop in polya_peaks[1:]:
        if start - new_polya_peaks[-1][-1] < 20:
            new_polya_peaks[-1][-1] = stop
        else:
            new_polya_peaks.append( [start, stop] )
    
    return new_polya_peaks


def find_cage_peaks_in_gene( ( chrm, strand ), gene, cage_cov, rnaseq_cov ):
    # threshold the CAGE data. We assume that the CAGE data is a mixture of 
    # reads taken from actually capped transcripts, and random transcribed 
    # regions, or RNA seq covered regions. We zero out any bases where we
    # can't reject the null hypothesis that the observed CAGE reads all derive 
    # from the background, at alpha = 0.001. 
    rnaseq_cov = numpy.array( rnaseq_cov+1-1e-6, dtype=int)
    max_val = rnaseq_cov.max()
    thresholds = TOTAL_MAPPED_READS*beta.ppf( 
        0.999, 
        numpy.arange(max_val+1)+1, 
        numpy.zeros(max_val+1)+(TOTAL_MAPPED_READS+1) 
    )
    max_scores = thresholds[ rnaseq_cov ]
    cage_cov[ cage_cov < max_scores ] = 0    
    
    
    raw_peaks = find_peaks( cage_cov, window_len=CAGE_PEAK_WIN_SIZE, 
                            min_score=MIN_NUM_CAGE_TAGS,
                            max_score_frac=MAX_CAGE_FRAC,
                            max_num_peaks=100)
    
    cage_peaks = Bins( chrm, strand )
    if len( raw_peaks ) == 0:
        return cage_peaks
    
    for peak_st, peak_sp in raw_peaks:
        # make sure there is *some* rnaseq coverage post peak
        #if rnaseq_cov[peak_st:peak_sp+100].sum() < MIN_NUM_CAGE_TAGS: continue
        # make sure that there is an increase in coverage from pre to post peak
        #pre_peak_cov = rnaseq_cov[peak_st-100:peak_st].sum()
        #post_peak_cov = rnaseq_cov[peak_st:peak_sp+100].sum()
        #if post_peak_cov/(pre_peak_cov+1e-6) < 5: continue
        cage_peaks.append( Bin( peak_st, peak_sp+1,
                                "CAGE_PEAK_START", "CAGE_PEAK_STOP", "CAGE_PEAK") )
    return cage_peaks

def find_polya_peaks_in_gene( ( chrm, strand ), gene, polya_cov, rnaseq_cov ):
    # threshold the polya data. We assume that the polya data is a mixture of 
    # reads taken from actually capped transcripts, and random transcribed 
    # regions, or RNA seq covered regions. We zero out any bases where we
    # can't reject the null hypothesis that the observed polya reads all derive 
    # from the background, at alpha = 0.001. 
    """
    rnaseq_cov = numpy.array( rnaseq_cov+1-1e-6, dtype=int)
    max_val = rnaseq_cov.max()
    thresholds = TOTAL_MAPPED_READS*beta.ppf( 
        0.1, 
        numpy.arange(max_val+1)+1, 
        numpy.zeros(max_val+1)+(TOTAL_MAPPED_READS+1) 
    )
    max_scores = thresholds[ rnaseq_cov ]
    polya_cov[ polya_cov < max_scores ] = 0    
    """
    
    raw_peaks = find_peaks( polya_cov, window_len=30, 
                            min_score=5,
                            max_score_frac=0.05,
                            max_num_peaks=100)
    polya_sites = Bins( chrm, strand )
    if len( raw_peaks ) == 0:
        return polya_sites
    
    for peak_st, peak_sp in raw_peaks:
        polya_bin = Bin( peak_st, peak_sp+1,
                         "POLYA_PEAK_START", "POLYA_PEAK_STOP", "POLYA")
        polya_sites.append( polya_bin )
    
    return polya_sites

def find_peaks( cov, window_len, min_score, max_score_frac, max_num_peaks ):    
    def overlaps_prev_peak( new_loc ):
        for start, stop in peaks:
            if not( new_loc > stop or new_loc + window_len < start ):
                return True
        return False
    
    # merge the peaks
    def grow_peak( start, stop, grow_size=
                   max(1, window_len/4), min_grow_ratio=0.2 ):
        # grow a peak at most max_num_peaks times
        max_mean_signal = cov[start:stop+1].mean()
        for i in xrange(max_num_peaks):
            curr_signal = cov[start:stop+1].sum()
            if curr_signal < min_score:
                return ( start, stop )
            
            downstream_sig = float(cov[max(0, start-grow_size):start].sum())/grow_size
            upstream_sig = float(cov[stop+1:stop+1+grow_size].sum())/grow_size
            
            # if neither passes the threshold, then return the current peak
            if max(upstream_sig, downstream_sig) \
                    < min_grow_ratio*curr_signal/float(stop-start+1): 
                return (start, stop)
            
            # if the expansion isn't greater than the min ratio, then return
            if max(upstream_sig,downstream_sig) < MAX_CAGE_FRAC*max_mean_signal:
                return (start, stop)
            
            # otherwise, we know one does
            if upstream_sig > downstream_sig:
                stop += grow_size
            else:
                start = max(0, start - grow_size )
        
        if VERBOSE:
            log_statement( 
                "Warning: reached max peak iteration at %i-%i ( signal %.2f )"
                    % (start, stop, cov[start:stop+1].sum() ) )
        return (start, stop )
    
    peaks = []
    peak_scores = []
    
    cumsum_cvg_array = \
        numpy.append(0, numpy.cumsum( cov ))
    scores = cumsum_cvg_array[window_len:] - cumsum_cvg_array[:-window_len]
    indices = numpy.argsort( scores )
    min_score = max( min_score, MAX_CAGE_FRAC*scores[ indices[-1] ] )
    for index in reversed(indices):
        if not overlaps_prev_peak( index ):
            score = scores[ index ]
            new_peak = grow_peak( index, index + window_len )
            # if we are below the minimum score, then we are done
            if score < min_score:
                break
            
            # if we have observed peaks, and the ratio between the highest
            # and the lowest is sufficeintly high, we are done
            if len( peak_scores ) > 0:
                if float(score)/peak_scores[0] < max_score_frac:
                    break
                        
            peaks.append( new_peak ) 
            peak_scores.append( score )
    
    if len( peaks ) == 0:
        return []
    
    # merge cage peaks together
    def merge_peaks( peaks_and_scores ):
        peaks_and_scores = sorted( list(x) for x in peaks_and_scores )
        peak, score = peaks_and_scores.pop()
        new_peaks = [peak,]
        new_scores = [score,]
        while len(peaks_and_scores) >  0:
            last_peak = new_peaks[-1]
            peak, score = peaks_and_scores.pop()
            new_peak = (min(peak[0], last_peak[0]), max(peak[1], last_peak[1]))
            if (new_peak[1] - new_peak[0]) <= 1.5*( 
                    last_peak[1] - last_peak[0] + peak[1] - peak[0] ):
                new_peaks[-1] = new_peak
                new_scores[-1] += score
            else:
                new_peaks.append( peak )
                new_scores.append( score )
        
        return zip( new_peaks, new_scores )
    
    peaks_and_scores = sorted( zip(peaks, peak_scores) )
    
    for i in xrange( 99 ):
        if i == 100: assert False
        old_len = len( peaks_and_scores )
        peaks_and_scores = merge_peaks( peaks_and_scores )
        if len( peaks_and_scores ) == old_len: break
    
        
    new_peaks_and_scores = []
    scores = (cumsum_cvg_array[3:] - cumsum_cvg_array[:-3])/3.
    for peak, score in peaks_and_scores:
        peak_scores = scores[peak[0]:peak[1]+1]
        max_score = peak_scores.max()
        good_indices = (peak_scores >= max_score*math.sqrt(MAX_CAGE_FRAC)).nonzero()[0]
        new_peak = [
                peak[0] + int(good_indices.min() + 1), 
                peak[0] + int(good_indices.max() + 2)  ]
        new_score = float(
            cumsum_cvg_array[new_peak[1]+1] - cumsum_cvg_array[new_peak[0]])
        new_peaks_and_scores.append( (new_peak, new_score) )
    
    peaks_and_scores = sorted( new_peaks_and_scores )
    max_score = max( s for p, s in peaks_and_scores )
    return [ pk for pk, score in peaks_and_scores \
                 if score >= MAX_CAGE_FRAC*max_score
                 and score > min_score ]


def find_left_exon_extensions( start_index, start_bin, gene_bins, rnaseq_cov ):
    internal_exons = []
    ee_indices = []
    start_bin_cvg = start_bin.mean_cov( rnaseq_cov )
    for i in xrange( start_index-1, 0, -1 ):
        bin = gene_bins[i]
        
        # break at canonical introns
        if bin.type == 'INTRON':
            break
        
        # make sure the average coverage is high enough
        bin_cvg = bin.mean_cov(rnaseq_cov)   
        if bin_cvg < MIN_EXON_BPKM:
            break

        if bin.stop - bin.start > 20 and \
                (start_bin_cvg+1e-6)/(bin_cvg+1e-6) > EXON_EXT_CVG_RATIO_THRESH:
            break
                
        # update the bin coverage. In cases where the coverage increases from
        # the canonical exon, we know that the increase is due to inhomogeneity
        # so we take the conservative choice
        start_bin_cvg = max( start_bin_cvg, bin_cvg )
        
        ee_indices.append( i )
        internal_exons.append( Bin( bin.start, bin.stop, 
                                    bin.left_label, bin.right_label, 
                                    "EXON_EXT"  ) )
    
    return internal_exons

def find_right_exon_extensions( start_index, start_bin, gene_bins, rnaseq_cov,
                                min_ext_ratio=EXON_EXT_CVG_RATIO_THRESH, 
                                min_bpkm=MIN_EXON_BPKM):
    exons = []
    ee_indices = []
    start_bin_cvg = start_bin.mean_cov( rnaseq_cov )
    for i in xrange( start_index+1, len(gene_bins) ):
        bin = gene_bins[i]

        # if we've reached a canonical intron, break
        if bin.type == 'INTRON':
            break
        
        # make sure the average coverage is high enough
        bin_cvg = bin.mean_cov(rnaseq_cov)
        if bin_cvg < min_bpkm:
            break
        
        if bin.stop - bin.start > 20 and \
                (start_bin_cvg+1e-6)/(bin_cvg+1e-6) > min_ext_ratio:
            break
                
        # update the bin coverage. In cases where the coverage increases from
        # the canonical exon, we know that the increase is due to inhomogeneity
        # so we take the conservative choice
        start_bin_cvg = max( start_bin_cvg, bin_cvg )

        ee_indices.append( i )
                
        exons.append( Bin( bin.start, bin.stop, 
                           bin.left_label, bin.right_label, 
                           "EXON_EXT"  ) )
    
    return exons


def build_labeled_segments( (chrm, strand), rnaseq_cov, jns, 
                            transcript_bndries=[] ):
    locs = defaultdict(set)    
    for start, stop in find_empty_regions( rnaseq_cov ):
        if stop - start < MIN_EMPTY_REGION_LEN: continue
        locs[start].add( "ESTART" )
        locs[stop].add( "ESTOP" )
    
    for start, stop, cnt in jns:
        if start < 1 or stop > len(rnaseq_cov): continue
        #assert start-1 not in locs, "%i in locs" % (start-1)
        locs[start-1].add( "D_JN" )
        locs[stop+1].add( "R_JN" )

    for bndry in sorted(transcript_bndries, reverse=True):
        locs[ bndry ].add( "TRANS_BNDRY" )
    
    # build all of the bins
    poss = sorted( locs.iteritems() )
    poss = merge_empty_labels( poss )
    if len( poss ) == 0: 
        return Bins( chrm, strand )

    if poss[0][0] > 1:
        poss.insert( 0, (1, set(["GENE_BNDRY",])) )
    if poss[-1][0] < len(rnaseq_cov)-1:
        poss.append( (len(rnaseq_cov)-1, set(["GENE_BNDRY",])) )
    
    bins = Bins( chrm, strand )
    for index, ((start, left_labels), (stop, right_labels)) in \
            enumerate(izip(poss[:-1], poss[1:])):
        for left_label in left_labels:
            for right_label in right_labels:
                bin_type = ( "INTRON" if left_label == 'D_JN' 
                             and right_label == 'R_JN' else None )
                bins.append(Bin(start, stop, left_label, right_label, bin_type))
    
    return bins

def find_canonical_and_internal_exons( (chrm, strand), rnaseq_cov, jns ):
    bins = build_labeled_segments( (chrm, strand), rnaseq_cov, jns )    
    
    def iter_canonical_exons_and_indices():
        for i, bin in enumerate( bins ):
            if bin.left_label == 'R_JN' and bin.right_label == 'D_JN':
                yield i, bin
    
    canonical_exons = Bins( chrm, strand )
    internal_exons = Bins( chrm, strand )
    for ce_i, ce_bin in iter_canonical_exons_and_indices():
        ce_bin.type = 'EXON'
        canonical_exons.append( ce_bin )
        internal_exons.append( ce_bin )
        
        for r_ext in find_right_exon_extensions(
                ce_i, ce_bin, bins, rnaseq_cov):
            exon = copy(ce_bin)
            exon.right_label = r_ext.right_label
            exon.stop = r_ext.stop
            internal_exons.append( exon )

        for l_ext in find_left_exon_extensions(
                ce_i, ce_bin, bins, rnaseq_cov):
            exon = copy(ce_bin)
            exon.left_label = l_ext.left_label
            exon.start = l_ext.start
            internal_exons.append( exon )
    
    return canonical_exons, internal_exons

def find_se_genes( 
        (chrm, strand), rnaseq_cov, jns, cage_peaks, polya_sites  ):
    bins = build_labeled_segments( 
        (chrm, strand), rnaseq_cov, jns, transcript_bndries=polya_sites )
    se_genes = Bins( chrm, strand )
    if len(bins) == 0: 
        return se_genes
    
    for peak in cage_peaks:
        # find all bins that start with a CAGE peak, and 
        # end with a polya or junction. because it's possible
        # for CAGE peaks to span splice donors, we need to 
        # look at all boundaries inside of the peak
        for i, bin in enumerate(bins):
            # continue until we've reched an overlapping bin
            if bin.stop < peak.start: continue
            # if we've moved past the peak, stop searching
            if bin.start > peak.stop: break
            # if the right label isn't a poly_bin, it's not a single exon gene
            if bin.right_label not in ( 'TRANS_BNDRY', ): continue
            # we know that we have a CAGE peak that lies ( at least partially )
            # within a bin that ends with a polya, so we've found a single exon 
            # gene
            se_gene = Bin( 
                peak.start, bin.stop, 
                "CAGE_PEAK", "POLYA", "SE_GENE" )
            se_genes.append( se_gene )
    
    return se_genes

def find_gene_bndry_exons( (chrm, strand), rnaseq_cov, jns, peaks, peak_type ):
    if peak_type == 'CAGE':
        stop_labels = ['D_JN', ]
        start_label = 'CAGE_PEAK'
        exon_label = 'TSS_EXON'
        reverse_bins = False
    elif peak_type == 'POLYA_SEQ':
        stop_labels = ['R_JN', ]
        start_label = 'POLYA_PEAK'
        exon_label = 'TES_EXON'
        reverse_bins = True
    else:
        assert False, "Unrecognized peak type '%s'" % peak_type

    bins = build_labeled_segments( (chrm, strand), rnaseq_cov, jns )
    if reverse_bins: 
        bins = bins.reverse_strand(len(rnaseq_cov))
        peaks = peaks.reverse_strand(len(rnaseq_cov))
    
    exons = []
    for peak in peaks:
        # find all bins that start with a peak, and 
        # end with a junction. because it's possible
        # for peaks to span boundaries, ( ie, CAGE
        # peaks can span spice donors ) we need to 
        # look at all boundaries inside of the peak
        bndry_exon_indices_and_bins = []
        for i, bin in enumerate(bins):
            if bin.stop < peak.start: continue
            if bin.right_label not in stop_labels: continue
            if bin.stop > peak.start:
                bndry_exon_indices_and_bins.append( (i, bin) )
            if bin.stop > peak.stop: break
        
        # for each start bin ( from the previous step )
        # we look for contigous signal. 
        for index, bndry_bin in bndry_exon_indices_and_bins:
            exon = Bin( peak.start, bndry_bin.stop, 
                        start_label, bndry_bin.right_label, exon_label )
            exons.append( exon )
            
            for r_ext in find_right_exon_extensions(
                    index, bndry_bin, bins, rnaseq_cov):
                exon = Bin( 
                    peak.start, r_ext.stop, 
                    start_label, r_ext.right_label, exon_label )
                exons.append( exon )
    
    bins = Bins( chrm, strand, sorted(set(exons)))
    if reverse_bins: bins = bins.reverse_strand(len(rnaseq_cov))
    
    return bins

def find_exons_in_gene( ( chrm, strand, contig_len ), gene, 
                        rnaseq_reads, cage_reads, polya_reads,
                        jns, cage_peaks=[], polya_peaks=[] ):
    ###########################################################
    # Shift all of the input data to be in the gene region, and 
    # reverse it when necessary
    
    ## FIX ME
    #polya_sites = [x - gene.start for x in polya_sites
    #               if x > gene.start and x <= gene.stop]
    gene_len = gene.stop - gene.start + 1
    jns = [ (x1-gene.start, x2-gene.start, cnt)  
            for x1, x2, cnt in jns ]
    cage_peaks = [ (x1-gene.start, x2-gene.start)
                   for x1, x2 in cage_peaks ]
    polya_peaks = [ (x1-gene.start, x2-gene.start)
                    for x1, x2 in polya_peaks ]
    for start, stop in cage_peaks:
        assert 0 <= start <= stop
    rnaseq_cov = rnaseq_reads.build_read_coverage_array( 
        chrm, strand, gene.start, gene.stop )
    
    if strand == '-':
        jns = [ (gene_len-x2, gene_len-x1, cnt) for x1, x2, cnt in jns ]
        cage_peaks = [ (gene_len-x2, gene_len-x1) for x1, x2 in cage_peaks ]
        polya_peaks = [ (gene_len-x2, gene_len-x1) for x1, x2 in polya_peaks ]
        rnaseq_cov = rnaseq_cov[::-1]

    polya_peaks = filter_polya_peaks(polya_peaks, rnaseq_cov, jns)
    
    
    filtered_junctions = []
    for (start, stop, cnt) in jns:
        if start < 0 or stop >= gene_len: continue
        left_intron_cvg = rnaseq_cov[start+10:start+30].sum()/20
        right_intron_cvg = rnaseq_cov[stop-30:stop-10].sum()/20        
        if cnt*10 < left_intron_cvg or cnt*10 < right_intron_cvg:
            continue
        filtered_junctions.append( (start, stop, cnt) )

    jns = filtered_junctions
    
    ### END Prepare input data #########################################

    # initialize the cage peaks with the reference provided set
    cage_peaks = Bins( chrm, strand, (
        Bin(pk_start, pk_stop+1, "CAGE_PEAK_START","CAGE_PEAK_STOP","CAGE_PEAK")
        for pk_start, pk_stop in cage_peaks ))
    if cage_reads != None:
        cage_cov = cage_reads.build_read_coverage_array( 
            chrm, strand, gene.start, gene.stop )
        if strand == '-': cage_cov = cage_cov[::-1]
        cage_peaks.extend( find_cage_peaks_in_gene( 
            (chrm, strand), gene, cage_cov, rnaseq_cov ) )
    
    # initialize the polya peaks with the reference provided set
    polya_peaks = Bins( chrm, strand, (
       Bin( pk_start, pk_stop+1, "POLYA_PEAK_START", "POLYA_PEAK_STOP", "POLYA")
        for pk_start, pk_stop in polya_peaks ))
    if polya_reads != None:
        polya_cov = polya_reads.build_read_coverage_array( 
            chrm, strand, gene.start, gene.stop )
        if strand == '-': polya_cov = polya_cov[::-1]
        polya_peaks.extend( find_polya_peaks_in_gene( 
            (chrm, strand), gene, polya_cov, rnaseq_cov ) )
    
    canonical_exons, internal_exons = find_canonical_and_internal_exons(
        (chrm, strand), rnaseq_cov, jns)
    tss_exons = find_gene_bndry_exons(
        (chrm, strand), rnaseq_cov, jns, cage_peaks, "CAGE")
    tes_exons = find_gene_bndry_exons(
        (chrm, strand), rnaseq_cov, jns, polya_peaks, "POLYA_SEQ")
    
    polya_sites = numpy.array(sorted(x.stop-1 for x in polya_peaks))
    se_genes = find_se_genes( 
        (chrm, strand), rnaseq_cov, jns, cage_peaks, polya_sites )
    
    gene_bins = Bins(chrm, strand, build_labeled_segments( 
            (chrm, strand), rnaseq_cov, jns ) )
    
    jn_bins = Bins(chrm, strand, [])
    for start, stop, cnt in jns:
        if stop - start <= 0:
            log_statement( "BAD JUNCTION: %s %s %s" % (start, stop, cnt) )
            continue
        bin = Bin(start, stop, 'R_JN', 'D_JN', 'INTRON', cnt)
        jn_bins.append( bin )
    
    # skip the first 200 bases to account for the expected lower coverage near 
    # the transcript bounds
    tss_exons = filter_exons(tss_exons, rnaseq_cov, 
                             num_start_bases_to_skip=NUM_TSS_BASES_TO_SKIP)
    tes_exons = filter_exons(tes_exons, rnaseq_cov, 
                             num_stop_bases_to_skip=NUM_TES_BASES_TO_SKIP)
    internal_exons = filter_exons( internal_exons, rnaseq_cov )
    se_genes = filter_exons( se_genes, rnaseq_cov, 
                             num_start_bases_to_skip=200, 
                             num_stop_bases_to_skip=400 )

    elements = Bins(chrm, strand, chain(
            jn_bins, cage_peaks, polya_peaks, 
            tss_exons, internal_exons, tes_exons, se_genes) )
    if strand == '-':
        elements = elements.reverse_strand( gene.stop - gene.start + 1 )
    elements = elements.shift( gene.start )
    elements.append( gene )

    if strand == '-':
        gene_bins = gene_bins.reverse_strand( gene.stop - gene.start + 1 )
    gene_bins = gene_bins.shift( gene.start )
    
    return elements, gene_bins
        

def find_exons_worker( (genes_queue, genes_queue_lock), ofp, 
                       (chrm, strand, contig_len),
                       jns, rnaseq_reads, cage_reads, polya_reads,
                       ref_elements ):
    jn_starts = [ i[0][0] for i in jns ]
    jn_stops = [ i[0][1] for i in jns ]
    jn_values = [ i[1] for i in jns ]

    def extract_elements_for_gene( gene ):
        # find the junctions associated with this gene
        gj_sa = bisect( jn_stops, gene.start )
        gj_so = bisect( jn_starts, gene.stop )
        gene_jns = zip( jn_starts[gj_sa:gj_so], 
                        jn_stops[gj_sa:gj_so], 
                        jn_values[gj_sa:gj_so] )
        
        gene_ref_elements = defaultdict(list)
        for key, vals in ref_elements.iteritems():
            if len( vals ) == 0: continue
            for start, stop in sorted(vals):
                if stop < gene.start: continue
                if start > gene.stop: break
                gene_ref_elements[key].append((start, stop))
        
        return gene_jns, gene_ref_elements
    
    rnaseq_reads = rnaseq_reads.reload()
    cage_reads = cage_reads.reload() if cage_reads != None else None
    polya_reads = polya_reads.reload() if polya_reads != None else None
    
    while True:
        log_statement( "Waiting for genes queue lock" )
        if genes_queue_lock != None:
            i = 1
            while not genes_queue_lock.acquire(timeout=1.0):
                log_statement( "Waited %.2f sec for gene queue lock" % i )
                i += 1
            if len(genes_queue) == 0:
                genes_queue_lock.release()
                break
            gene = genes_queue.pop()
            genes_queue_lock.release()
        else:
            assert NTHREADS == 1
            if len( genes_queue ) == 0: 
                break
            gene = genes_queue.pop()
    
        log_statement( "Finding Exons in Chrm %s Strand %s Pos %i-%i" % 
                       (chrm, strand, gene.start, gene.stop) )

        gene_jns, gene_ref_elements = extract_elements_for_gene( gene )
        elements, pseudo_exons = find_exons_in_gene(
            ( chrm, strand, contig_len ), gene, 
            rnaseq_reads, cage_reads, polya_reads, gene_jns,
            gene_ref_elements['promoters'], 
            gene_ref_elements['polya'])
        
        # merge in the reference elements
        for tss_exon in gene_ref_elements['tss_exons']:
            elements.append( Bin(tss_exon[0], tss_exon[1], 
                                 "REF_TSS_EXON_START", "REF_TSS_EXON_STOP",
                                 "TSS_EXON") )
        for tes_exon in gene_ref_elements['tes_exons']:
            elements.append( Bin(tes_exon[0], tes_exon[1], 
                                 "REF_TES_EXON_START", "REF_TES_EXON_STOP",
                                 "TES_EXON") )
        write_unified_bed( elements, ofp)
        
        if WRITE_DEBUG_DATA:
            pseudo_exons.writeBed( ofp )
        
        log_statement( "FINISHED Finding Exons in Chrm %s Strand %s Pos %i-%i" %
                       (chrm, strand, gene.start, gene.stop) )
    
    log_statement( "" )
    return

def extract_reference_elements(genes, ref_elements_to_include, strand):
    ref_elements = defaultdict(set)
    if not any(ref_elements_to_include):
        return ref_elements
    
    for gene in genes:
        elements = gene.extract_elements()
        if ref_elements_to_include.junctions:
            ref_elements['introns'].update(elements['intron'])
        if ref_elements_to_include.promoters:
            ref_elements['promoters'].update(elements['promoter'])
        if ref_elements_to_include.polya_sites:
            ref_elements['polya'].update(elements['polya'])
        if ref_elements_to_include.TSS:
            ref_elements['tss_exons'].update(elements['tss_exon'])
        if ref_elements_to_include.TES:
            ref_elements['tes_exons'].update(elements['tes_exon'])
    
    for key, val in ref_elements.iteritems():
        ref_elements[key] = sorted( val )

    return ref_elements

def find_exons_in_contig( (chrm, strand, contig_len), ofp,
                          rnaseq_reads, cage_reads, polya_reads,
                          ref_genes, ref_elements_to_include):
    assert not any(ref_elements_to_include) or ref_genes != None
    
    gene_bndry_bins = None
    if ref_elements_to_include.genes == True:
        assert ref_genes != None
        gene_bndry_bins = load_gene_bndry_bins(
            ref_genes, chrm, strand, contig_len)
        if len( gene_bndry_bins ) == 0:
            return
    
    # load junctions from the RNAseq data
    junctions = load_junctions( rnaseq_reads, cage_reads, polya_reads, 
                                (chrm, strand, contig_len) )
    # load the reference elements
    ref_elements = extract_reference_elements( 
        ref_genes, ref_elements_to_include, strand )
    
    # update the junctions with the reference junctions, and sort them
    for jn in ref_elements['introns']:
        junctions[jn] += 0
    junctions = sorted( junctions.iteritems() )
    # del introns from the reference elements because they've already been 
    # merged into the set of junctions
    del ref_elements['introns']
    
    if gene_bndry_bins == None:
        log_statement( "Finding gene boundaries in contig '%s' on '%s' strand" 
                       % ( chrm, strand ) )
        gene_bndry_bins = find_gene_boundaries( 
            (chrm, strand, contig_len), rnaseq_reads, 
            cage_reads, polya_reads, junctions )
    
    log_statement( "Finding exons in contig '%s' on '%s' strand" 
                   % ( chrm, strand ) )
    if NTHREADS > 1:
        manager = multiprocessing.Manager()
        genes_queue = manager.list()
        genes_queue_lock = multiprocessing.Lock()
    else:
        genes_queue, genes_queue_lock = [], None
        
    genes_queue.extend( gene_bndry_bins )
    sorted_jns = sorted( junctions )
    args = [ (genes_queue, genes_queue_lock), ofp, (chrm, strand, contig_len),
              sorted_jns, rnaseq_reads, cage_reads, polya_reads, ref_elements ]
    
    #global NTHREADS
    #NTHREADS = 1
    if NTHREADS == 1:
        find_exons_worker(*args)
    else:
        log_statement( "Waiting on exon finding children in contig '%s' on '%s' strand" % ( chrm, strand ) )
        ps = []
        for i in xrange( min(NTHREADS, MAX_THREADS_PER_CONTIG) ):
            p = multiprocessing.Process(target=find_exons_worker, args=args)
            p.start()
            ps.append( p )
        
        while True:
            if all( not p.is_alive() for p in ps ):
                break
            time.sleep( 0.1 )

    log_statement( "" )    
    return


def load_gene_bndry_bins( genes, contig, strand, contig_len ):
    log_statement( "Loading gene boundaries from annotated genes in %s:%s" % (
            contig, strand) )

    ## find the gene regions in this contig. Note that these
    ## may be overlapping
    gene_intervals = []
    for gene in genes:
        if gene.chrm != contig: continue
        if gene.strand != strand: continue
        gene_intervals.append((gene.start, gene.stop))

    ## merge overlapping genes regions by building a graph with nodes
    ## of all gene regions, and edges with all overlapping genes 

    # first, find the edges by probing into the sorted intervals
    gene_intervals.sort()
    gene_starts = numpy.array([interval[0] for interval in gene_intervals])
    overlapping_genes = []
    for gene_index, (start, stop) in enumerate(gene_intervals):
        start_i = numpy.searchsorted(gene_starts, start)
        # start looping over potentially overlapping intervals
        for i, gene_interval in enumerate(gene_intervals[start_i:]):
            # if we have surpassed all potentially overlapping intervals,
            # then we don't need to go any further
            if gene_interval[0] > stop: break
            # if the intervals overlap ( I dont think I need this test, but
            # it's cheap and this could be an insidious bug )
            if not (stop < gene_interval[0] or start > gene_interval[1] ):
                overlapping_genes.append( (int(gene_index), int(i+start_i)) )
    
    # buld the graph, find the connected components, and build 
    # the set of merged intervals
    genes_graph = Graph(len(gene_starts))
    genes_graph.add_edges(overlapping_genes)
    merged_gene_intervals = []
    for genes in genes_graph.clusters():
        start = min( gene_intervals[i][0] for i in genes )
        stop = max( gene_intervals[i][1] for i in genes )
        merged_gene_intervals.append( [start, stop] )
    
    # expand the gene boundaries to their maximum amount such that the genes 
    # aren't overlapping. This is to allow for gene ends that lie outside of 
    # the previously annotated boundaries
    merged_gene_intervals.sort()
    for i in xrange(1,len(merged_gene_intervals)-1):
        mid = (merged_gene_intervals[i][1]+merged_gene_intervals[i+1][0])/2
        merged_gene_intervals[i][1] = int(mid)-1
        merged_gene_intervals[i+1][0] = int(mid)+1    
    merged_gene_intervals[0][0] = max( 
        1, merged_gene_intervals[0][0]-MAX_GENE_EXPANSION)
    merged_gene_intervals[-1][1] = min( 
        contig_len-1, merged_gene_intervals[-1][1]+MAX_GENE_EXPANSION)

    # build gene objects with the intervals
    gene_bndry_bins = []
    for start, stop in merged_gene_intervals:
        gene_bin = Bin(start, stop, 'GENE', 'GENE', 'GENE')
        gene_bndry_bins.append( gene_bin )
    
    log_statement( "" )
    
    return gene_bndry_bins

def parse_arguments():
    import argparse

    parser = argparse.ArgumentParser(\
        description='Find exons from RNAseq, CAGE, and poly(A) assays.')

    parser.add_argument( 
        '--rnaseq-reads', type=argparse.FileType('rb'), required=True, 
        help='BAM file containing mapped RNAseq reads.')
    parser.add_argument( '--rnaseq-read-type', required=True,
        choices=["forward", "backward"],
        help='Whether or not the first RNAseq read in a pair needs to be reversed to be on the correct strand.')
    parser.add_argument( '--num-mapped-rnaseq-reads', type=int,
        help="The total number of mapped rnaseq reads ( needed to calculate the FPKM ). This only needs to be set if it isn't found by a call to samtools idxstats." )
    
    parser.add_argument( '--cage-reads', type=argparse.FileType('rb'),
        help='BAM file containing mapped cage reads.')
    parser.add_argument( '--rampage-reads', type=argparse.FileType('rb'),
        help='BAM file containing mapped rampage reads.')

    parser.add_argument( '--polya-reads', type=argparse.FileType('rb'),
        help='BAM file containing mapped polya reads.')
    
    parser.add_argument( '--reference', help='Reference GTF')
    parser.add_argument( '--use-reference-genes', 
                         help='Use genes boundaries from the reference annotation.', 
                         default=False, action='store_true')
    parser.add_argument( '--use-reference-junctions', 
                         help='Include junctions from the reference annotation.',
                         default=False, action='store_true')
    parser.add_argument( '--use-reference-tss', 
                         help='Use TSS\'s taken from the reference annotation.',
                         default=False, action='store_true')
    parser.add_argument( '--use-reference-tes', 
                         help='Use TES\'s taken from the reference annotation.',
                         default=False, action='store_true')
    parser.add_argument( '--use-reference-promoters', 
                         help='Use promoters\'s inferred from the start of reference transcripts.',
                         default=False, action='store_true')
    parser.add_argument( '--use-reference-polyas', 
                         help='Use polya sites inferred from the end of reference transcripts.',
                         default=False, action='store_true')
    
    parser.add_argument( '--ofname', '-o', 
                         default="discovered.elements.bed",\
        help='Output file name. (default: discovered.elements.bed)')
    
    parser.add_argument( '--verbose', '-v', default=False, action='store_true',
        help='Whether or not to print status information.')
    parser.add_argument( '--debug-verbose', default=False, action='store_true',
        help='Whether or not to print debugging information.')
    parser.add_argument('--write-debug-data',default=False,action='store_true',
        help='Whether or not to print out gff files containing intermediate exon assembly data.')

    parser.add_argument( '--ucsc', default=False, action='store_true',
        help='Try to format contig names in the ucsc format (typically by prepending a chr).')    
    parser.add_argument( '--batch-mode', '-b', 
        default=False, action='store_true',
        help='Disable the ncurses frontend, and just print status messages to stderr.')
    
    parser.add_argument( '--threads', '-t', default=1, type=int,
        help='The number of threads to use.')
        
    args = parser.parse_args()

    global NTHREADS
    NTHREADS = args.threads
    global MAX_THREADS_PER_CONTIG
    MAX_THREADS_PER_CONTIG = NTHREADS/2 if NTHREADS > 20 else NTHREADS
    
    global WRITE_DEBUG_DATA
    WRITE_DEBUG_DATA = args.write_debug_data
    
    global VERBOSE
    VERBOSE = args.verbose
    global DEBUG_VERBOSE
    DEBUG_VERBOSE = args.debug_verbose

    global TOTAL_MAPPED_READS
    TOTAL_MAPPED_READS = ( None if args.num_mapped_rnaseq_reads == None 
                           else args.num_mapped_rnaseq_reads )
    
    global FIX_CHRM_NAMES_FOR_UCSC
    FIX_CHRM_NAMES_FOR_UCSC = args.ucsc
    
    if None == args.reference and args.use_reference_genes:
        raise ValueError, "--reference must be set if --use-reference-genes is set"
    if None == args.reference and args.use_reference_junctions:
        raise ValueError, "--reference must be set if --use-reference-junctions is set"
    if None == args.reference and args.use_reference_tss:
        raise ValueError, "--reference must be set if --use-reference-tss is set"
    if None == args.reference and args.use_reference_tes:
        raise ValueError, "--reference must be set if --use-reference-tes is set"
    if None == args.reference and args.use_reference_promoters:
        raise ValueError, "--reference must be set if --use-reference-promoters is set"
    if None == args.reference and args.use_reference_polyas:
        raise ValueError, "--reference must be set if --use-reference-polyas is set"
    RefElementsToInclude = namedtuple(
        'RefElementsToInclude', 
        ['genes', 'junctions', 'TSS', 'TES', 'promoters', 'polya_sites'])
    ref_elements_to_include = RefElementsToInclude(args.use_reference_genes, 
                                                   args.use_reference_junctions,
                                                   args.use_reference_tss, 
                                                   args.use_reference_tes,
                                                   args.use_reference_promoters,
                                                   args.use_reference_polyas)
    
    ofp = ThreadSafeFile( args.ofname, "w" )
    ofp.write('track name="%s" visibility=2 itemRgb="On"\n' % ofp.name)
    
    if ( args.cage_reads == None 
         and args.rampage_reads == None 
         and not args.use_reference_tss
         and not args.use_reference_promoters):
        raise ValueError, "--cage-reads or --rampage-reads or --use-reference-tss or --use-reference-promoters must be set"
    if args.cage_reads != None and args.rampage_reads != None:
        raise ValueError, "--cage-reads and --rampage-reads may not both be set"
    
    if ( args.polya_reads == None 
         and not args.use_reference_tes
         and not args.use_reference_polyas ):
        raise ValueError, "Either --polya-reads or --use-reference-tes or --use-reference-polyas must be set"
    
    reverse_rnaseq_strand = ( 
        True if args.rnaseq_read_type == 'backward' else False )
    
    return args.rnaseq_reads, reverse_rnaseq_strand, \
        args.cage_reads, args.rampage_reads, args.polya_reads, \
        ofp, args.reference, ref_elements_to_include, \
        not args.batch_mode


def load_promoter_reads(cage_bam, rampage_bam):
    if cage_bam != None:
        if VERBOSE: log_statement( 'Loading CAGE read bams' )
        return CAGEReads(cage_bam.name).init(
            reverse_read_strand=True)
    
    if rampage_bam != None:
        if VERBOSE: log_statement( 'Loading RAMPAGE read bams' )            
        return RAMPAGEReads(rampage_bam.name).init(
            reverse_read_strand=True)
    
    return None

def main():
    ( rnaseq_bam, reverse_rnaseq_strand, cage_bam, rampage_bam, polya_bam,
      ofp, ref_gtf_fname, ref_elements_to_include, 
      use_ncurses ) = parse_arguments()
    
    global log_statement
    log_ofstream = open( ".".join(ofp.name.split(".")[:-1]) + ".log", "w" )
    log_statement = Logger(
        nthreads=NTHREADS+max(1,(NTHREADS/MAX_THREADS_PER_CONTIG)), 
        use_ncurses=use_ncurses, log_ofstream=log_ofstream)

    # wrap everything in a try block so that we can with elegantly handle
    # uncaught exceptions
    try:
        if VERBOSE: log_statement( 'Loading RNAseq read bams' )                
        rnaseq_reads = RNAseqReads(rnaseq_bam.name).init(
            reverse_read_strand=reverse_rnaseq_strand)

        global TOTAL_MAPPED_READS
        if TOTAL_MAPPED_READS == None:
            TOTAL_MAPPED_READS = rnaseq_reads.mapped
            if TOTAL_MAPPED_READS == 0:
                raise ValueError, "Can't determine the number of reads in the RNASeq BAM (by samtools idxstats). Please set --num-mapped-rnaseq-reads"
        assert TOTAL_MAPPED_READS > 0
        
        if VERBOSE: log_statement( 'Loading promoter reads bams' )        
        promoter_reads = load_promoter_reads(cage_bam, rampage_bam)

        if VERBOSE: log_statement( 'Loading polyA reads bams' )        
        polya_reads = ( PolyAReads(polya_bam.name).init(
                reverse_read_strand=True, pairs_are_opp_strand=True) 
                        if polya_bam != None else None )
        
        contig_lens = get_contigs_and_lens( 
            [ reads for reads in [rnaseq_reads, promoter_reads, polya_reads]
              if reads != None ] )
        
        if any( ref_elements_to_include ):
            if VERBOSE: log_statement("Loading annotation file.")
            genes = load_gtf( ref_gtf_fname )
        else:
            genes = []
        
        # Call the children processes
        all_args = []
        for contig, contig_len in contig_lens.iteritems():
            #if contig != '20': continue
            for strand in '+-':
                contig_genes = [ 
                    gene for gene in genes 
                    if gene.strand == strand and gene.chrm == contig ]
                # skip this contig if we are only using reference genes and
                # this gene is not in the contig
                if ref_elements_to_include.genes and len(contig_genes) == 0:
                    continue
                all_args.append( ( 
                        (contig, strand, contig_len), ofp,
                        rnaseq_reads, promoter_reads, polya_reads, 
                        contig_genes, ref_elements_to_include) )
        
        if NTHREADS == MAX_THREADS_PER_CONTIG:
            for args in all_args:
                find_exons_in_contig(*args)
        else:
            log_statement( 'Waiting on children processes.' )
            # max MAX_THREADS_PER_CONTIG threads per process
            n_simulataneous_contigs = ( 
                1 if MAX_THREADS_PER_CONTIG == NTHREADS else 2 )
            ps = [None]*n_simulataneous_contigs
            while len(all_args) > 0:
                for i, p in enumerate(ps):
                    if p == None or not p.is_alive():
                        args = all_args.pop()
                        p = multiprocessing.Process( 
                            target=find_exons_in_contig, args=args )
                        p.start()
                        ps[i] = p
                        break
                time.sleep(0.1)

            while True:
                if all( p == None or not p.is_alive() for p in ps ):
                    break
                time.sleep( 0.1 )
    except Exception, inst:
        log_statement( "FATAL ERROR" )
        log_statement( traceback.format_exc() )
        log_ofstream.close()
        log_statement.close()
        raise
    else:
        log_ofstream.close()
        log_statement.close()
    
if __name__ == '__main__':
    main()

#
