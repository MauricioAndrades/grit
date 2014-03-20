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

import networkx as nx

from files.reads import MergedReads, RNAseqReads, CAGEReads, RAMPAGEReads, \
    PolyAReads, \
    clean_chr_name, fix_chrm_name_for_ucsc, get_contigs_and_lens, \
    guess_strand_from_fname, iter_coverage_intervals_for_read
import files.junctions
from files.bed import create_bed_line
from files.gtf import parse_gtf_line, load_gtf

from lib.logging import Logger
import lib.arg_parsing
from lib.arg_parsing import initialize_reads_from_args

import config

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
    regions = sorted(regions)
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
    if type(new_regions[0]) in (int, numpy.int64):
        return [new_regions]
    else:
        return new_regions

def build_empty_array():
    return numpy.array(())

def cluster_segments( segments, jns ):
    if len(segments) == 0:
        return []
    segments = flatten(segments)
    boundaries = numpy.array(sorted(chain(*segments)))
    edges = set()
    for (start, stop), cnt in jns:
        if start-1 < boundaries[0]: continue
        if stop+1 >= boundaries[-1]: continue
        start_bin = boundaries.searchsorted( start-1, side='right' )-1
        assert start_bin >= 0
        stop_bin = boundaries.searchsorted( stop+1, side='right' )-1
        assert stop_bin < len(boundaries)-1
        if start_bin != stop_bin:
            edges.add((int(min(start_bin, stop_bin)), 
                       int(max(start_bin, stop_bin))))
    
    genes_graph = nx.Graph()
    genes_graph.add_nodes_from(xrange(len(boundaries)-1))
    try:
        genes_graph.add_edges_from(edges)
    except:
        print config.log_statement( str(boundaries), log=True )
        print config.log_statement( str(edges), log=True )
        raise
    
    segments = []
    for g in nx.connected_components(genes_graph):
        g = sorted(g)
        segments.append( (boundaries[min(g)]+1, boundaries[max(g)+1]-1) )
    segments.sort()
    
    return flatten(segments)

def cluster_segments_2( boundaries, jns ):
    if len(boundaries) < 2:
        return []
    
    boundaries = numpy.array(sorted(boundaries))
    edges = set()
    for start, stop, cnt in jns:
        if start-1 < boundaries[0]: continue
        if stop+1 >= boundaries[-1]: continue
        start_bin = boundaries.searchsorted( start-1, side='right' )-1
        assert start_bin >= 0
        stop_bin = boundaries.searchsorted( stop+1, side='right' )-1
        assert stop_bin < len(boundaries)-1
        if start_bin != stop_bin:
            edges.add((int(min(start_bin, stop_bin)), 
                       int(max(start_bin, stop_bin))))
    
    genes_graph = nx.Graph()
    genes_graph.add_nodes_from(xrange(len(boundaries)-1) ) # XXX shouyld this be len -1?
    genes_graph.add_edges_from( list( edges ) )

    segments = []
    for g in nx.connected_components(genes_graph):
        g = sorted(g)
        segment = []
        prev_i = g[0]
        segment.append( [boundaries[prev_i]+1, ])
        for i in g[1:]:
            # if we've skipped at least one node, then add
            # a new region onto this segment
            if i > prev_i + 1:
                segment[-1].append( boundaries[prev_i+1]-1 )
                segment.append( [boundaries[i]+1, ])
                prev_i = i
            # otherwise, we've progressed to an adjacent sergments
            # so just merge the adjacent intervals
            else:
                assert i == prev_i + 1
                prev_i += 1
        segment[-1].append( boundaries[g[-1]+1]-1 )
        
        segments.append(segment)
    
    return segments


def find_empty_regions( cov, thresh=1e-6, 
                        min_length=config.MAX_EMPTY_REGION_SIZE ):
    x = numpy.diff( numpy.asarray( cov >= thresh, dtype=int ) )
    stops = numpy.nonzero(x==1)[0].tolist()
    if cov[-1] < thresh: stops.append(len(x))
    starts = numpy.nonzero(x==-1)[0].tolist()
    if cov[0] < thresh: starts.insert(0, 0)
    assert len(starts) == len(stops)
    return [ x for x in izip(starts, stops) if x[1]-x[0]+1 > min_length ]

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

def filter_exon(exon, wig, num_start_bases_to_skip=0, num_stop_bases_to_skip=0):
    '''Find all the exons that are sufficiently homogenous and expressed.
    
    '''
    start = exon.start + num_start_bases_to_skip
    end = exon.stop - num_stop_bases_to_skip
    if end - start < config.MIN_EXON_SIZE: return False
    vals = wig[start:end+1]
    n_div = max( 1, int(len(vals)/config.MAX_EMPTY_REGION_SIZE) )
    div_len = len(vals)/n_div
    for i in xrange(n_div):
        seg = vals[i*div_len:(i+1)*div_len]
        if seg.mean() < config.MIN_EXON_BPKM:
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
        assert stop - start >= 0
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
            if self.type =='INTERGENIC_SPACE':
                return '254,254,34'
        
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
        'POLYA': 'polya',
        'INTERGENIC_SPACE': 'intergenic'
    }

    color_mapping = { 
        'GENE': '200,200,200',
        'CAGE_PEAK': '153,255,000',
        'SE_GENE': '000,000,200',
        'TSS_EXON': '140,195,59',
        'EXON': '000,000,000',
        'TES_EXON': '255,51,255',
        'INTRON': '100,100,100',
        'POLYA': '255,0,0',
        'INTERGENIC_SPACE': '254,254,34'
    }

    chrm = elements.chrm
    if config.FIX_CHRM_NAMES_FOR_UCSC:
        chrm = fix_chrm_name_for_ucsc(chrm)

    for element in elements:
        region = ( chrm, elements.strand, element.start, element.stop)

        if isinstance(element, Bin):
            blocks = []
            use_thick_lines=(element.type != 'INTRON')
            element_type = element.type
            score = element.score
        elif isinstance(element, Bins):
            # subtract 1 because beds are 0-based
            blocks = [(x.start-1, x.stop-1) for x in element]
            use_thick_lines = True
            element_type = 'GENE'
            score = 1000
        else: assert False

        grp_id = element_type + "_%s_%s_%i_%i" % region

        # subtract 1 because we work in 1 based coords, but beds are 0-based
        # also, add 1 to stop because beds are open-closed ( which means no net 
        # change for the stop coordinate )
        bed_line = create_bed_line( chrm, elements.strand, 
                                    element.start-1, element.stop, 
                                    feature_mapping[element_type],
                                    score=score,
                                    color=color_mapping[element_type],
                                    use_thick_lines=use_thick_lines,
                                    blocks=blocks)
        ofp.write( bed_line + "\n"  )
    
    return

class Bins( list ):
    def __init__( self, chrm, strand, iter=[] ):
        self.chrm = chrm
        self.strand = strand
        self.extend( iter )
        if config.FIX_CHRM_NAMES_FOR_UCSC:
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
    
    @property
    def start(self):
        return min( x.start for x in self )

    @property
    def stop(self):
        return max( x.stop for x in self )
    
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
            if config.FIX_CHRM_NAMES_FOR_UCSC:
                chrm = fix_chrm_name_for_ucsc(self.chrm)
            region = GenomicInterval(chrm, self.strand, 
                                     bin.start, bin.stop)
            grp_id = "%s_%s_%i_%i" % region
            ofp.write( create_gff_line(region, grp_id) + "\n" )
        
        return

def load_and_filter_junctions( rnaseq_reads, promoter_reads, polya_reads, 
                               regions, nthreads, ratio_filter, max_jn_size ):
    # load and filter the ranseq reads. We can't filter all of the reads because
    # they are on differnet scales, so we only filter the RNAseq and use the 
    # cage and polya to get connectivity at the boundaries.
    rnaseq_junctions = files.junctions.load_junctions_in_bam(
        rnaseq_reads, regions, nthreads=nthreads)
    promoter_jns = None if promoter_reads == None else \
        files.junctions.load_junctions_in_bam(promoter_reads, regions, nthreads)
    polya_jns = None if polya_reads == None else \
        files.junctions.load_junctions_in_bam(polya_reads, regions, nthreads)
    
    if config.VERBOSE: config.log_statement("Filtering junctions")
    filtered_junctions = defaultdict(lambda: defaultdict(int))
    for chrm, strand, region_start, region_stop in regions:        
        # filter junctions
        jn_starts = defaultdict( int )
        jn_stops = defaultdict( int )
        for (start, stop), cnt in rnaseq_junctions[(chrm, strand)]:
            jn_starts[start] = max( jn_starts[start], cnt )
            jn_stops[stop] = max( jn_stops[stop], cnt )
        
        for (start, stop), cnt in rnaseq_junctions[(chrm, strand)]:
            if (float(cnt)+1)/jn_starts[start] < 0.01: continue
            if (float(cnt)+1)/jn_stops[stop] < 0.01: continue
            if stop - start + 1 > config.MAX_INTRON_SIZE: continue
            filtered_junctions[(chrm, strand)][(start, stop)] = cnt
        
        del rnaseq_junctions[(chrm, strand)]
        
        # add in the cage and polya reads, for connectivity
        for connectivity_jns in (promoter_jns, polya_jns):
            if connectivity_jns == None: continue
            for jn, cnt in connectivity_jns[(chrm, strand)]:
                filtered_junctions[(chrm, strand)][jn] += 0
            del connectivity_jns[(chrm, strand)]
    
    return filtered_junctions

def load_junctions( rnaseq_reads, promoter_reads, polya_reads,
                    regions, nthreads):
    return load_and_filter_junctions( 
        rnaseq_reads, promoter_reads, polya_reads, 
        regions, nthreads=nthreads, ratio_filter=0.01,
        max_jn_size=config.MAX_INTRON_SIZE)
    

def find_initial_segmentation_worker( 
        candidate_boundaries, candidate_boundaries_lock,
        accepted_boundaries, accepted_boundaries_lock,
        chrm, strand, 
        rnaseq_reads, cage_reads, polya_reads ):
    config.log_statement( "Finding Segments (%s:%s)" % ( chrm, strand ))
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
    
    config.log_statement("Putting Segments in Queue (%s, %s)" % (chrm, strand))
    with accepted_boundaries_lock:
        accepted_boundaries.append( locs )
    config.log_statement( "" )
    return

def find_gene_boundaries((chrm, strand, contig_len), 
                         rnaseq_reads, cage_reads, polya_reads,
                         ref_elements, ref_elements_to_include,
                         junctions, nthreads=config.NTHREADS):
    def find_segments_with_signal( chrm, strand, rnaseq_reads ):
        # initialize a tiling of segments acfross the genome to check for signal
        # This is expensive, so we farm it out to worker processes. But, 
        # the simple algorithm would be to 
        manager = multiprocessing.Manager()
        candidate_segments = manager.list()
        candidate_segments_lock = manager.Lock()
        for middle in xrange( config.MAX_EMPTY_REGION_SIZE/2, 
                              contig_len-config.MAX_EMPTY_REGION_SIZE/2, 
                              config.MAX_EMPTY_REGION_SIZE ):
            
            candidate_segments.append( 
                (middle-config.MAX_EMPTY_REGION_SIZE/2,
                 middle+config.MAX_EMPTY_REGION_SIZE/2))
        
        accepted_segments = manager.list()
        accepted_segments_lock = manager.Lock()

        args = (candidate_segments, candidate_segments_lock,
                accepted_segments, accepted_segments_lock,
                chrm, strand, rnaseq_reads, cage_reads, polya_reads )
        
        if config.NTHREADS == 1:
            find_initial_segmentation_worker(*args)
        else:
            ps = []
            for i in xrange(nthreads):
                p = multiprocessing.Process( 
                    target=find_initial_segmentation_worker, args=args)
                p.start()
                ps.append( p )

            # store the initial number of segments for logging
            n_bndries = len(candidate_segments)
            while True:
                config.log_statement(
                    "Waiting on segmentation children in %s:%s (%i/%i remain)" 
                    % (chrm, strand, len(candidate_segments), n_bndries) )

                if all( not p.is_alive() for p in ps ):
                    break
                time.sleep( 0.5 )

        config.log_statement( "Merging segments queue in %s:%s" 
                       % ( chrm, strand ) )        

        locs = []
        with accepted_segments_lock:
            for bndries in accepted_segments:
                locs.extend( bndries )
        if len(locs) == 0:
            return locs
                
        # merge adjoining segments
        locs.sort()
        new_locs = [locs.pop(0),]
        for start, stop in locs:
            if start == new_locs[-1][1]:
                new_locs[-1] = (new_locs[-1][0], stop)
            else:
                new_locs.append( (start, stop) )
                
        config.log_statement( "Finished segmentation in %s:%s" 
                       % ( chrm, strand ) )
        return new_locs
            

    config.log_statement("Finding gene boundaries in contig '%s' on '%s' strand"
                   % ( chrm, strand ) )
        
    if junctions == None:
        # load junctions from the RNAseq data
        junctions = load_junctions( 
            rnaseq_reads, cage_reads, polya_reads, 
            [(chrm, strand, 0, contig_len),], nthreads )[(chrm, strand)]
    
    # update the junctions with the reference junctions, and sort them
    if ref_elements_to_include.junctions:
        for jn in ref_elements['intron']:
            junctions[jn] += 0
    junctions = sorted( junctions.iteritems() )
    
    # find segment boundaries
    if config.VERBOSE: config.log_statement( 
        "Finding segments for %s:%s" % (chrm, strand) )
    segments = find_segments_with_signal(chrm, strand, rnaseq_reads)
    
    # because the segments are disjoint, they are implicitly merged
    merged_segments = segments
    
    if config.VERBOSE: 
        config.log_statement( "Clustering segments for %s %s" % (chrm, strand))
    clustered_segments = cluster_segments( merged_segments, junctions )
    
    # if we didn't find any genes, then return nothing
    if len( clustered_segments ) == 2:
        return []
    
    # build the gene bins
    genes = []
    for start, stop in clustered_segments:
        #if stop - start < config.MIN_GENE_LENGTH: continue
        gene = Bins( chrm, strand, [] )
        gene.append( Bin(max(1,start-10), min(stop+10,contig_len), 
                          "ESTART", "ESTOP", "GENE" ) )
        genes.append( gene  )
    
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
    thresholds = config.TOTAL_MAPPED_READS*beta.ppf( 
        config.CAGE_FILTER_ALPHA, 
        numpy.arange(max_val+1)+1, 
        numpy.zeros(max_val+1)+(config.TOTAL_MAPPED_READS+1) 
    )
    max_scores = thresholds[ rnaseq_cov ]
    cage_cov[ cage_cov < max_scores ] = 0    
    
    
    raw_peaks = find_peaks( cage_cov, window_len=config.CAGE_PEAK_WIN_SIZE, 
                            min_score=config.MIN_NUM_CAGE_TAGS,
                            max_score_frac=config.MAX_CAGE_FRAC,
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
                            min_score=config.MIN_NUM_POLYA_TAGS,
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
                   max(1, window_len/4), min_grow_ratio=config.MAX_CAGE_FRAC ):
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
            if max(upstream_sig,downstream_sig) < \
                    config.MAX_CAGE_FRAC*max_mean_signal:
                return (start, stop)
            
            # otherwise, we know one does
            if upstream_sig > downstream_sig:
                stop += grow_size
            else:
                start = max(0, start - grow_size )
        
        if config.VERBOSE:
            config.log_statement( 
                "Warning: reached max peak iteration at %i-%i ( signal %.2f )"
                    % (start, stop, cov[start:stop+1].sum() ) )
        return (start, stop )
    
    peaks = []
    peak_scores = []
    cumsum_cvg_array = (
        numpy.append(0, numpy.cumsum( cov )) )
    scores = cumsum_cvg_array[window_len:] - cumsum_cvg_array[:-window_len]
    indices = numpy.argsort( scores )
    min_score = max( min_score, config.MAX_CAGE_FRAC*scores[ indices[-1] ] )
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
        good_indices = (peak_scores >= max_score*config.MAX_CAGE_FRAC).nonzero()[0]
        new_peak = [
                peak[0] + int(good_indices.min() + 1), 
                peak[0] + int(good_indices.max() + 2)  ]
        new_score = float(
            cumsum_cvg_array[new_peak[1]+1] - cumsum_cvg_array[new_peak[0]])
        new_peaks_and_scores.append( (new_peak, new_score) )
    
    peaks_and_scores = sorted( new_peaks_and_scores )
    max_score = max( s for p, s in peaks_and_scores )
    return [ pk for pk, score in peaks_and_scores \
                 if score >= config.MAX_CAGE_FRAC*max_score
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
        if bin_cvg < config.MIN_EXON_BPKM:
            break

        if bin.stop - bin.start > 20 and \
                (start_bin_cvg+1e-6)/(bin_cvg+1e-6) > \
                config.EXON_EXT_CVG_RATIO_THRESH:
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
                                min_ext_ratio=config.EXON_EXT_CVG_RATIO_THRESH, 
                                min_bpkm=config.MIN_EXON_BPKM):
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
        (chrm, strand), rnaseq_cov, jns, cage_peaks, polya_peaks  ):
    polya_sites = numpy.array(sorted(x.stop-1 for x in polya_peaks))
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

def find_intergenic_space( 
        (chrm, strand), rnaseq_cov, jns, cage_peaks, polya_peaks  ):
    cage_peak_starts = numpy.array(sorted(x.start for x in cage_peaks))
    bins = build_labeled_segments( 
        (chrm, strand), rnaseq_cov, jns, transcript_bndries=cage_peak_starts )
    intergenic_segments = Bins( chrm, strand )
    if len(bins) == 0: 
        return intergenic_segments
    
    for peak in polya_peaks:
        # find all bins that start with a polya site, and 
        # end with a cage peak.
        for i, bin in enumerate(bins):
            # continue until we've reched an overlapping bin
            if bin.stop <= peak.stop: continue
            # if we've moved past the peak, stop searching
            if bin.start > peak.stop: break
            # if the right label isn't a cage_peak, it's not intergenic space
            if bin.right_label not in ( 'TRANS_BNDRY', ): continue
            intergenic_bin = Bin( 
                peak.stop, bin.stop,
                "POLYA", "CAGE_PEAK", "INTERGENIC_SPACE" )
            intergenic_segments.append( intergenic_bin )
            # intergenic space only extends to the first cage peak
            break
    
    return intergenic_segments


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

def re_segment_gene( gene, (chrm, strand, contig_len),
                     rnaseq_cov, jns, cage_peaks, polya_peaks):
    # find long emtpy regions, that we may have missed int he previous scan
    empty_regions = find_empty_regions( 
        rnaseq_cov, thresh=1e-6, min_length=config.MIN_GENE_LENGTH )
    
    # find intergenic bins ( those that start with polya signal,
    # and end with cage signal )
    intergenic_bins = find_intergenic_space(
        (chrm, strand), rnaseq_cov, jns, cage_peaks, polya_peaks )
    
    # find the empty space inside of these, but with a much shorter
    # allowed distance because of the additional information
    for intergenic_bin in intergenic_bins:
        if intergenic_bin.stop - intergenic_bin.start + 1 < 10:
            continue
        for start, stop in find_empty_regions( 
                rnaseq_cov[intergenic_bin.start+1:intergenic_bin.stop-1] ):
            empty_regions.append( 
                (intergenic_bin.start+start, intergenic_bin.start+stop) )
    
    if len(empty_regions) == 0:
        return [gene,]
    
    split_points = []
    for start, stop in flatten(empty_regions):
        split_points.append( int((start+stop)/2) )
    
    if len(split_points) == 0:
        return [gene,]
    split_points.insert(0, 0 )
    split_points.append(gene.stop-gene.start+1 )
    split_points.sort()
    intervals = cluster_segments_2( split_points, jns )
    if len(intervals) <= 1: 
        return [gene,]
    
    # add the intergenic space, since there could be genes in the interior
    new_genes = []
    for segments in intervals: 
        new_gene = Bins( gene.chrm, gene.strand )
        for start, stop in segments:
            new_gene.append( Bin(start, stop, "ESTART","ESTOP","GENE") )
        if gene.stop-gene.start+1 < config.MIN_GENE_LENGTH: continue
        if strand == '-': 
            new_gene = new_gene.shift(contig_len-gene.stop).reverse_strand(contig_len)
        else:
            new_gene = new_gene.shift(gene.start)
        new_genes.append(new_gene)
    
    return new_genes

def filter_jns(jns, rnaseq_cov, gene):
    filtered_junctions = []
    for (start, stop, cnt) in jns:
        if start < 0 or stop >= gene.stop - gene.start + 1: continue
        if stop - start + 1 > config.MAX_INTRON_SIZE : continue
        left_intron_cvg = rnaseq_cov[start+10:start+30].sum()/20
        right_intron_cvg = rnaseq_cov[stop-30:stop-10].sum()/20        
        if (cnt+1)*10 < left_intron_cvg or (cnt+1)*10 < right_intron_cvg:
            continue
        filtered_junctions.append( (start, stop, cnt) )
    
    return filtered_junctions

def find_coverage_in_gene(gene, reads):
    cov = numpy.zeros(gene.stop-gene.start+1, dtype=float)
    for x in gene:
        seg_cov = reads.build_read_coverage_array( 
            gene.chrm, gene.strand, x.start, x.stop )
        cov[x.start-gene.start:x.stop-gene.start+1] = seg_cov
    if gene.strand == '-': cov = cov[::-1]
    return cov

def build_raw_elements_in_gene( gene, 
                                rnaseq_reads, cage_reads, polya_reads,
                                ref_cage_peaks, ref_jns, ref_polya_peaks  ):
    """Find the read coverage arrays, junctions lists,
    and make the appropriate conversion into gene coordiantes.
    
    """
    def points_are_inside_gene(start, stop):
        start_inside = any( x.start <= start <= x.stop for x in gene )
        stop_inside = any( x.start <= stop <= x.stop for x in gene )
        return start_inside and stop_inside
    
    cage_cov, polya_cov = None, None
    
    gene_len = gene.stop - gene.start + 1
    jns = load_junctions(rnaseq_reads, cage_reads, polya_reads,
                         [(gene.chrm, gene.strand, gene.start, gene.stop),],
                         nthreads=1)[(gene.chrm, gene.strand)]    
    # merge in the reference junctions
    for start, stop in ref_jns:
        jns[(start,stop)] += 0
    
    jns = [ (x1-gene.start, x2-gene.start, cnt)  
            for (x1, x2), cnt in sorted(jns.iteritems())
            if points_are_inside_gene(x1, x2)]
    
    cage_peaks = [ (x1-gene.start, x2-gene.start)
                   for x1, x2 in ref_cage_peaks 
                   if points_are_inside_gene(x1, x2)]
    assert all( 0 <= start <= stop for start, stop in cage_peaks )
    
    if cage_reads != None:
        cage_cov = find_coverage_in_gene(gene, cage_reads)
    
    polya_peaks = [ (x1-gene.start, x2-gene.start)
                    for x1, x2 in ref_polya_peaks
                    if points_are_inside_gene(x1, x2)]
    assert all( 0 <= start <= stop for start, stop in polya_peaks )
    
    if polya_reads != None:
        polya_cov = find_coverage_in_gene(gene, polya_reads)
    
    rnaseq_cov = find_coverage_in_gene( gene, rnaseq_reads )
    
    if gene.strand == '-':
        jns = [ (gene_len-x2, gene_len-x1, cnt) for x1, x2, cnt in jns ]
        cage_peaks = [ (gene_len-x2, gene_len-x1) for x1, x2 in cage_peaks ]
        polya_peaks = [ (gene_len-x2, gene_len-x1) for x1, x2 in polya_peaks ]
    
    polya_peaks = filter_polya_peaks(polya_peaks, rnaseq_cov, jns)
    
    jns = filter_jns(jns, rnaseq_cov, gene)

    return rnaseq_cov, cage_cov, cage_peaks, polya_cov, polya_peaks, jns

def find_exons_in_gene( gene, contig_len,
                        rnaseq_reads, cage_reads, polya_reads,
                        cage_peaks=[], introns=[], polya_peaks=[],
                        resplit_genes=True):
    assert isinstance( gene, Bins )
    
    config.log_statement( "Finding Exons in Chrm %s Strand %s Pos %i-%i" % 
                   (gene.chrm, gene.strand, gene.start, gene.stop) )
    
    ###########################################################
    # Shift all of the input data to be in the gene region, and 
    # reverse it when necessary
    
    ( rnaseq_cov, cage_cov, cage_peaks, polya_cov, polya_peaks,
      jns) =  build_raw_elements_in_gene( 
        gene, rnaseq_reads, cage_reads, polya_reads, 
        cage_peaks, introns, polya_peaks)
    ### END Prepare input data #########################################
    
    # initialize the cage peaks with the reference provided set
    cage_peaks = Bins( gene.chrm, gene.strand, (
        Bin(pk_start, pk_stop+1, "CAGE_PEAK_START","CAGE_PEAK_STOP","CAGE_PEAK")
        for pk_start, pk_stop in cage_peaks ))
    if cage_reads != None:
        cage_peaks.extend( find_cage_peaks_in_gene( 
            (gene.chrm, gene.strand), gene, cage_cov, rnaseq_cov ) )
    
    # initialize the polya peaks with the reference provided set
    polya_peaks = Bins( gene.chrm, gene.strand, (
       Bin( pk_start, pk_stop+1, "POLYA_PEAK_START", "POLYA_PEAK_STOP", "POLYA")
        for pk_start, pk_stop in polya_peaks ))
    if polya_reads != None:
        polya_peaks.extend( find_polya_peaks_in_gene( 
            (gene.chrm, gene.strand), gene, polya_cov, rnaseq_cov ) )
    
    ## TODO - re-enable to give cleaner gene boundaries. As is, we'll provide
    ## boundaries for regions in which we can not produce transcripts
    #if len(cage_peaks) == 0 or len(polya_peaks) == 0:
    #    return Bins(gene.chrm, gene.strand), None, None
    
    if resplit_genes and len(gene) == 1 and (
            len(cage_peaks) > 0 and len(polya_peaks) > 0):
        new_genes = re_segment_gene( 
            gene, (gene.chrm, gene.strand, contig_len),
            rnaseq_cov, jns, cage_peaks, polya_peaks )
        if len(new_genes) > 1:
            return None, None, new_genes        
    
    se_genes = find_se_genes( 
        (gene.chrm, gene.strand), rnaseq_cov, jns, cage_peaks, polya_peaks )
    
    canonical_exons, internal_exons = find_canonical_and_internal_exons(
        (gene.chrm, gene.strand), rnaseq_cov, jns)
    tss_exons = find_gene_bndry_exons(
        (gene.chrm, gene.strand), rnaseq_cov, jns, cage_peaks, "CAGE")
    tes_exons = find_gene_bndry_exons(
        (gene.chrm, gene.strand), rnaseq_cov, jns, polya_peaks, "POLYA_SEQ")
    gene_bins = Bins(gene.chrm, gene.strand, build_labeled_segments( 
            (gene.chrm, gene.strand), rnaseq_cov, jns ) )
    
    jn_bins = Bins(gene.chrm, gene.strand, [])
    for start, stop, cnt in jns:
        if stop - start <= 0:
            config.log_statement("BAD JUNCTION: %s %s %s" % (start, stop, cnt))
            continue
        bin = Bin(start, stop, 'R_JN', 'D_JN', 'INTRON', cnt)
        jn_bins.append( bin )
    
    # skip the first 200 bases to account for the expected lower coverage near 
    # the transcript bounds
    tss_exons = filter_exons(
        tss_exons, rnaseq_cov, 
        num_start_bases_to_skip=config.NUM_TSS_BASES_TO_SKIP)
    tes_exons = filter_exons(
        tes_exons, rnaseq_cov, 
        num_stop_bases_to_skip=config.NUM_TES_BASES_TO_SKIP)
    internal_exons = filter_exons( internal_exons, rnaseq_cov )
    se_genes = filter_exons( 
        se_genes, rnaseq_cov, 
        num_start_bases_to_skip=config.NUM_TSS_BASES_TO_SKIP, 
        num_stop_bases_to_skip=config.NUM_TES_BASES_TO_SKIP )

    elements = Bins(gene.chrm, gene.strand, chain(
            jn_bins, cage_peaks, polya_peaks, 
            tss_exons, internal_exons, tes_exons, se_genes) )
    if gene.strand == '-':
        elements = elements.reverse_strand( gene.stop - gene.start + 1 )
    elements = elements.shift( gene.start )
    elements.append( gene )

    if gene.strand == '-':
        gene_bins = gene_bins.reverse_strand( gene.stop - gene.start + 1 )
    gene_bins = gene_bins.shift( gene.start )
    
    return elements, gene_bins, None
        

def find_exons_and_process_data(gene, contig_lens, ofp,
                                ref_elements, ref_elements_to_include,
                                rnaseq_reads, cage_reads, polya_reads):
    # extract the reference elements that we want to add in
    gene_ref_elements = defaultdict(list)
    for key, vals in ref_elements[(gene.chrm, gene.strand)].iteritems():
        if len( vals ) == 0: continue
        for start, stop in sorted(vals):
            if stop < gene.start: continue
            if start > gene.stop: break
            gene_ref_elements[key].append((start, stop))
    
    elements, pseudo_exons, new_gene_boundaries = find_exons_in_gene(
        gene, contig_lens[gene.chrm],
        rnaseq_reads, cage_reads, polya_reads, 
        gene_ref_elements['promoters'], 
        gene_ref_elements['intron'],
        gene_ref_elements['polya'],
        resplit_genes=(False == ref_elements_to_include.genes))

    if new_gene_boundaries != None:
        return new_gene_boundaries
    
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

    if config.WRITE_DEBUG_DATA:
        pseudo_exons.writeBed( ofp )
    
    config.log_statement( "FINISHED Finding Exons in Chrm %s Strand %s Pos %i-%i" %
                   (gene.chrm, gene.strand, gene.start, gene.stop) )
    return None

def find_exons_worker( (genes_queue, genes_queue_lock, n_threads_running), 
                       ofp, contig_lens, ref_elements, ref_elements_to_include,
                       rnaseq_reads, cage_reads, polya_reads ):
        
    rnaseq_reads = rnaseq_reads.reload()
    cage_reads = cage_reads.reload() if cage_reads != None else None
    polya_reads = polya_reads.reload() if polya_reads != None else None
    
    while True:
        # try to get a gene
        with genes_queue_lock:
            try: gene = genes_queue.pop()
            except IndexError: gene = None
        
        # if there is no gene it process, but threads are still running, then 
        # wait for the queue to fill or the process to finish
        if gene == None and n_threads_running.value > 0:
            config.log_statement( 
                "Waiting for gene to process (%i)" % n_threads_running.value)
            time.sleep(0.1)
            continue
        # otherwise, take a lock to make sure that the queue is empty and no 
        # threads are running. If so, then return
        elif gene == None:
            with genes_queue_lock:
                if len(genes_queue) == 0 and n_threads_running.value == 0:
                    config.log_statement( "" )
                    return
                else: continue

        # otherwise, we have a gene to process, so process it
        assert gene != None
        with genes_queue_lock: n_threads_running.value += 1

        # find the exons and genes
        rv = find_exons_and_process_data(gene, contig_lens, ofp,
                                         ref_elements, ref_elements_to_include,
                                         rnaseq_reads, cage_reads, polya_reads)
        
        # if the return value is new genes, then add these to the queue
        if rv != None:
            with genes_queue_lock:
                for gene in rv:
                    genes_queue.append( gene )
                n_threads_running.value -= 1
        # otherwise, we found the exons and wrote out the element data,
        # so just decrease the number of running threads
        else:
            with genes_queue_lock:
                n_threads_running.value -= 1
    
    assert False
    return

def extract_reference_elements(genes, ref_elements_to_include):
    ref_elements = defaultdict( lambda: defaultdict(set) )
    if not any(ref_elements_to_include):
        return ref_elements
    
    for gene in genes:
        elements = gene.extract_elements()
        # we add 1 to everything because find elements is 1 based, but 
        # extract elements is 0 based
        def add_elements(key):
            for start, stop in elements[key]:
                ref_elements[(gene.chrm, gene.strand)][key].add((start+1, stop+1))

        if ref_elements_to_include.junctions:
            add_elements('intron')
        if ref_elements_to_include.promoters:
            add_elements('promoter')
        if ref_elements_to_include.polya_sites:
            add_elements('polya')
        if ref_elements_to_include.TSS:
            add_elements('tss_exon')
        if ref_elements_to_include.TES:
            add_elements('tes_exon')
    
    for contig_strand, elements in ref_elements.iteritems():
        for element_type, val in elements.iteritems():
            ref_elements[contig_strand][element_type] = sorted( val )
    
    return ref_elements

def find_all_gene_segments( contig_lens, 
                            rnaseq_reads, promoter_reads, polya_reads,
                            ref_genes, ref_elements_to_include,
                            region_to_use=None,
                            junctions=None):
    assert not any(ref_elements_to_include) or ref_genes != None
    gene_bndry_bins = []
    
    # if we are supposed to use the annotation genes
    if ref_elements_to_include.genes == True:
        for contig, contig_len in contig_lens.iteritems():
            if region_to_use != None and contig != region_to_use: continue
            for strand in '+-':
                contig_gene_bndry_bins = load_gene_bndry_bins(
                    ref_genes, contig, strand, contig_len)
                gene_bndry_bins.extend( contig_gene_bndry_bins )
        return gene_bndry_bins
    
    # load the reference elements
    ref_elements = extract_reference_elements( 
        ref_genes, ref_elements_to_include )

    if junctions == None:
        regions = []
        for contig, contig_len in contig_lens.iteritems():
            if region_to_use != None and contig != region_to_use: continue
            for strand in '+-':
                regions.append( (contig, strand, 0, contig_len) )

        if config.VERBOSE: config.log_statement( 'Loading junctions' )        
        junctions = defaultdict(lambda: defaultdict(int))
        if ref_elements_to_include.junctions:
            for (chrm, strand), contig_ref_elements in ref_elements.iteritems():
                for jn in contig_ref_elements['intron']:
                    junctions[(chrm, strand)][jn] += 0
        
        discovered_junctions = load_junctions( 
            rnaseq_reads, promoter_reads, polya_reads, regions, config.NTHREADS)
        for (chrm, strand), contig_jns in discovered_junctions.iteritems():
            for jn, cnt in contig_jns.iteritems():
                junctions[(chrm, strand)][jn] += cnt                
        del discovered_junctions
        

    for contig, contig_len in contig_lens.iteritems():
        if region_to_use != None and contig != region_to_use: continue
        for strand in '+-':
            config.log_statement( "Finding gene boundaries in contig '%s' on '%s' strand" 
                           % ( contig, strand ) )
            contig_gene_bndry_bins = find_gene_boundaries( 
                (contig, strand, contig_len), 
                rnaseq_reads, promoter_reads, polya_reads, 
                ref_elements, ref_elements_to_include,
                junctions=junctions[(contig, strand)], nthreads=config.NTHREADS
            )
            gene_bndry_bins.extend( contig_gene_bndry_bins )
    
    return gene_bndry_bins

def find_exons( contig_lens, gene_bndry_bins, ofp,
                rnaseq_reads, cage_reads, polya_reads,
                ref_genes, ref_elements_to_include,
                junctions=None, nthreads=None):
    assert not any(ref_elements_to_include) or ref_genes != None
    if nthreads == None: nthreads = config.NTHREADS
    assert junctions == None
    
    ref_elements = extract_reference_elements( 
        ref_genes, ref_elements_to_include )
    
    genes_queue_lock = multiprocessing.Lock()
    threads_are_running = multiprocessing.Value('i', 0)
    if config.NTHREADS > 1:
        manager = multiprocessing.Manager()
        genes_queue = manager.list()
    else:
        genes_queue = []
     
    genes_queue.extend( gene_bndry_bins )
    args = [ (genes_queue, genes_queue_lock, threads_are_running), 
             ofp, contig_lens, ref_elements, ref_elements_to_include,
             rnaseq_reads, cage_reads, polya_reads  ]
    
    if nthreads == 1:
        find_exons_worker(*args)
    else:
        config.log_statement( "Waiting on exon finding children" )
        ps = []
        for i in xrange( nthreads ):
            p = multiprocessing.Process(target=find_exons_worker, args=args)
            p.start()
            ps.append( p )
        
        while True:
            if all( not p.is_alive() for p in ps ):
                break
            time.sleep( 0.1 )

    config.log_statement( "" )    
    return

def load_gene_bndry_bins( genes, contig, strand, contig_len ):
    config.log_statement( "Loading gene boundaries from annotated genes in %s:%s" % (
            contig, strand) )

    ## find the gene regions in this contig. Note that these
    ## may be overlapping
    gene_intervals = []
    for gene in genes:
        if gene.chrm != contig: continue
        if gene.strand != strand: continue
        gene_intervals.append((gene.start, gene.stop))
    if len(gene_intervals) == 0: return []
    
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
    genes_graph = nx.Graph()
    genes_graph.add_nodes_from(xrange(len(gene_starts)))
    genes_graph.add_edges_from(overlapping_genes)
    merged_gene_intervals = []
    for genes in nx.connected_components(genes_graph):
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
        1, merged_gene_intervals[0][0]-config.MAX_GENE_EXPANSION)
    merged_gene_intervals[-1][1] = min( 
        contig_len-1, merged_gene_intervals[-1][1]+config.MAX_GENE_EXPANSION)

    # build gene objects with the intervals
    gene_bndry_bins = []
    for start, stop in merged_gene_intervals:
        gene_bin = Bins( contig, strand, [
                Bin(start, stop, 'GENE', 'GENE', 'GENE'),] )
        gene_bndry_bins.append( gene_bin )
    
    config.log_statement( "" )
    
    return gene_bndry_bins

def find_elements( promoter_reads, rnaseq_reads, polya_reads,
                   ofname, ref_genes, ref_elements_to_include, 
                   region_to_use=None):
    # wrap everything in a try block so that we can with elegantly handle
    # uncaught exceptions
    try:
        ofp = ThreadSafeFile( ofname, "w" )
        ofp.write('track name="%s" visibility=2 itemRgb="On" useScore=1\n' \
                  % ofname)
        
        contigs, contig_lens = get_contigs_and_lens( 
            [ reads for reads in [rnaseq_reads, promoter_reads, polya_reads]
              if reads != None ] )
        contig_lens = dict(zip(contigs, contig_lens))
        
        regions = []
        for contig, contig_len in contig_lens.iteritems():
            if region_to_use != None and contig != region_to_use: continue
            for strand in '+-':
                regions.append( (contig, strand, 0, contig_len) )        
        
        # load the reference elements
        gene_segments = find_all_gene_segments( 
            contig_lens, 
            rnaseq_reads, promoter_reads, polya_reads,
            ref_genes, ref_elements_to_include, 
            region_to_use=region_to_use,
            junctions=None)
        
        # sort genes from longest to shortest. This should help improve the 
        # multicore performance
        gene_segments.sort( key=lambda x: x.stop-x.start, reverse=True )
                
        find_exons( contig_lens, gene_segments, ofp,
                    rnaseq_reads, promoter_reads, polya_reads,
                    ref_genes, ref_elements_to_include, 
                    junctions=None, nthreads=config.NTHREADS )            
    except Exception, inst:
        config.log_statement( "FATAL ERROR", log=True )
        config.log_statement( traceback.format_exc(), log=True, display=False )
        ofp.close()
        raise
    else:
        ofp.close()
    
    return ofname
