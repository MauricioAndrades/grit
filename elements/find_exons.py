# Copyright (c) 2011-2013 Nathan Boley

import sys, os
import numpy
import multiprocessing
from scipy import stats

from collections import OrderedDict, defaultdict
from itertools import izip, chain

import pygraph
from pygraph.classes.graph import graph
from pygraph.algorithms.accessibility import connected_components

sys.path.append(os.path.join(os.path.dirname(__file__), "../", 'file_types'))
from tabix_wiggle import Wiggle, guess_strand_from_fname
from junctions_file import parse_jn_gff, Junctions
from gtf_file import parse_gff_line, create_gff_line, GenomicInterval
from bed import create_bed_line

from bisect import bisect
from copy import copy

WRITE_DEBUG_DATA = False
EXON_EXT_CVG_RATIO_THRESH = 4
MIN_NUM_CAGE_TAGS = 10
MIN_EXON_BPKM = 1.0
MAX_CAGE_FRAC = 0.05

ofps_prefixes = [ "cage_peaks", "single_exon_genes", 
                  "tss_exons", "internal_exons", "tes_exons" ]

class ThreadSafeFile( file ):
    def __init__( self, fname, mode, trackname=None ):
        file.__init__( self, fname, mode )
        self._writelock = multiprocessing.Lock()
        if trackname != None:
            self.write('track name="%s" visibility=2 itemRgb="On"\n'%trackname)
    
    def write( self, data ):
        self._writelock.acquire()
        file.write( self, data )
        file.flush( self )
        self._writelock.release()

def multi_delete( l, is_to_remove ):
    for i in sorted( is_to_remove, reverse=True ):
        l.pop( i )
    return

def build_empty_array():
    return numpy.array(())

def find_polya_sites( polya_sites_fnames ):
    locs = defaultdict( list )
    for fname in polya_sites_fnames:
        strand = guess_strand_from_fname( fname )
        with open( fname ) as fp:
            for line in fp:
                if line.startswith( "track" ): continue
                data = line.split()
                chrm, start, stop, value = \
                    data[0], int(data[1]), int(data[2]), float(data[3])
                assert start == stop
                assert value == 1
                locs[(chrm, strand)].append( start )
    
    # convert to a dict of sorted numpy arrays
    numpy_locs = defaultdict( build_empty_array )

    for (chrm, strand), polya_sites in locs.iteritems():
        # make sure they're unique
        assert len( polya_sites ) == len( set( polya_sites ) )

        polya_sites.sort()
        if chrm.startswith( 'chr' ):
            chrm = chrm[3:]
        
        numpy_locs[(chrm, strand)] = numpy.array( polya_sites )
    
    return numpy_locs

class Bins( list ):
    def __init__( self, chrm, strand, iter=[] ):
        self.chrm = chrm
        self.strand = strand
        self.extend( iter )
        self._bed_template = "\t".join( ["chr"+chrm, '{start}', '{stop}', '{name}', 
                                         '1000', strand, '{start}', '{stop}', 
                                         '{color}']  ) + "\n"
        
    def reverse_strand( self, contig_len ):
        rev_bins = Bins( self.chrm, self.strand )
        for bin in reversed(self):
            rev_bins.append( bin.reverse_strand( contig_len ) )
        return rev_bins
    
    def writeBed( self, ofp, contig_len ):
        """
            chr7    127471196  127472363  Pos1  0  +  127471196  127472363  255,0,0
        """
        if self.strand == '-':
            writetable_bins = self.reverse_strand( contig_len )
        else:
            writetable_bins = self
        
        for bin in writetable_bins:
            length = max( (bin.stop - bin.start)/4, 1)
            colors = bin._find_colors( self.strand )
            if isinstance( colors, str ):
                op = self._bed_template.format(
                    start=bin.start-1,stop=bin.stop,color=colors, 
                    name="%s_%s"%(bin.left_label, bin.right_label) )
                ofp.write( op )
            else:
                op = self._bed_template.format(
                    start=bin.start,stop=(bin.start + length),color=colors[0], 
                    name=bin.left_label)
                ofp.write( op )
                
                op = self._bed_template.format(
                    start=(bin.stop-1-length),stop=bin.stop-1,color=colors[1],
                    name=bin.right_label)
                ofp.write( op )
        
        return

    def writeGff( self, ofp, contig_len, filter=None ):
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
            region = GenomicInterval(self.chrm, self.strand, 
                                     bin.start, bin.stop)
            grp_id = "%s_%s_%i_%i" % region
            ofp.write( create_gff_line(region, grp_id) + "\n" )
        
        return


def write_unified_bed( elements, contig_len, ofp ):
    assert isinstance( elements, Bins )
    
    feature_mapping = { 
        'CAGE_PEAK': 'promoter',
        'SE_GENE': 'single_exon_gene',
        'TSS_EXON': 'tss_exon',
        'EXON': 'internal_exon',
        'TES_EXON': 'tes_exon',
        'INTRON': 'intron'
    }

    color_mapping = { 
        'CAGE_PEAK': '153,255,000',
        'SE_GENE': '000,000,000',
        'TSS_EXON': '140,195,59',
        'EXON': '000,000,000',
        'TES_EXON': '255,51,255',
        'INTRON': '200,200,200'
    }
    
    if elements.strand == '-':
        writetable_bins = elements.reverse_strand( contig_len )
    else:
        writetable_bins = elements
    
    for bin in writetable_bins:
            region = GenomicInterval( elements.chrm, elements.strand, 
                                      bin.start, bin.stop)
            grp_id = feature_mapping[bin.type] + "_%s_%s_%i_%i" % region
            bed_line = create_bed_line( elements.chrm, elements.strand, 
                                        bin.start, bin.stop, 
                                        feature_mapping[bin.type],
                                        score=bin.score,
                                        color=color_mapping[bin.type],
                                        use_thick_lines=(bin.type != 'INTRON'))
            ofp.write( bed_line + "\n"  )
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
    
    def mean_cov( self, cov_array ):
        return cov_array[self.start:self.stop].mean()
    
    def reverse_strand(self, contig_len):
        return Bin(contig_len-self.stop, contig_len-self.start, 
                   self.right_label, self.left_label, self.type)
    
    def __repr__( self ):
        if self.type == None:
            return "%i-%i\t%s\t%s" % ( self.start, self.stop, self.left_label,
                                       self.right_label )

        return "%s:%i-%i" % ( self.type, self.start, self.stop )
    
    def __hash__( self ):
        return hash( (self.start, self.stop) )
    
    def __eq__( self, other ):
        return ( self.start == other.start and self.stop == other.stop )
    
    _bndry_color_mapping = {
        'CONTIG_BNDRY': '0,0,0',
        
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
        

def reverse_contig_data( rnaseq_cov, jns, cage_cov, polya_sites ):
    assert len( rnaseq_cov ) == len( cage_cov )
    genome_len = len( rnaseq_cov )
    rev_jns = dict( ((genome_len-stop-1, genome_len-start-1), value) 
                for (start, stop), value in jns.iteritems() )
    return ( rnaseq_cov[::-1], rev_jns, cage_cov[::-1], genome_len-polya_sites )

def check_for_se_gene( start, stop, cage_cov, rnaseq_cov ):
    # check to see if the rnaseq signal after the cage peak
    # is much greater than before
    split_pos = cage_cov[start:stop+1].argmax() + start
    if cage_cov[split_pos-1:split_pos+2].sum() < MIN_NUM_CAGE_TAGS:
        return False
    window_size = min( split_pos - start, stop - split_pos, 200 )
    after_cov = rnaseq_cov[split_pos:split_pos+window_size].sum()
    before_cov = rnaseq_cov[split_pos-window_size:split_pos].sum()
    if (after_cov/(before_cov+1)) > 5:
        return True
 
    return False

def find_gene_boundaries( (chrm, strand), cage_cov, rnaseq_cov, polya_sites, jns):
    locs = {}
    for polya in polya_sites:
        locs[ polya ] = "POLYA"
    
    for start, stop in jns:
        locs[start-1] = "D_JN"
        locs[stop+1] = "R_JN"
    
    for start, stop in find_empty_regions( rnaseq_cov ):
        if stop - start < MIN_EMPTY_REGION_LEN: continue
        if start in locs or stop in locs:
            continue
        locs[start] = "ESTART"
        locs[stop] = "ESTOP"
    
    # merge together empty bins
    # build all of the bins
    poss = sorted( locs.iteritems() )
    poss = merge_empty_labels( poss )
        
    bins = Bins( chrm, strand, [] )
    if poss[0][0] > 1:
        bins.append( Bin(1, poss[0][0], "CONTIG_BNDRY", poss[0][1]) )
    for index, ((start, left_label), (stop, right_label)) in \
            enumerate(izip(poss[:-1], poss[1:])):
        bins.append( Bin(start, stop, left_label, right_label) )
    
    if poss[-1][0] < len(rnaseq_cov)-1:
        bins.append( Bin( poss[-1][0], len(rnaseq_cov)-1, 
                          poss[-1][1], "CONTIG_BNDRY" ) )
    
    if WRITE_DEBUG_DATA:
        bins.writeBed( debug_fps['binsFps'][strand], len(rnaseq_cov) )
    
    # find regions that end with a polya, and look like genic regions
    # ( ie, they are a double polya that looks like a single exon gene, or
    # they start after a polya leading into a donor exon
    gene_starts_indices = []
    for i, bin in enumerate( bins ):
        if bin.left_label == 'POLYA' and bin.right_label in ('D_JN', 'ESTART', 'ESTOP'):
            gene_starts_indices.append( i )
        if bin.left_label in ('POLYA', 'ESTOP') and bin.right_label  == 'POLYA':
            if check_for_se_gene( bin.start, bin.stop, cage_cov, rnaseq_cov ):
                gene_starts_indices.append( i )
    
    # build a bins object from the initial labels
    gene_bndry_bins = Bins( chrm, strand )
    for start_i, stop_i in \
            zip( gene_starts_indices[:-1], gene_starts_indices[1:] ):
        start = bins[start_i].start
        left_label = bins[start_i].left_label
        stop = bins[stop_i-1].stop
        right_label = bins[stop_i-1].right_label
        gene_bndry_bins.append(
            Bin(start, stop, left_label, right_label, 'GENE'))

    if 0 == len( gene_bndry_bins  ):
        return gene_bndry_bins
    
    gene_bndry_bins[0].start = 1
    gene_bndry_bins[-1].stop = len( rnaseq_cov )-1
    
    # find the junctions that overlap multiple gene bins
    # we use the interval overlap join algorithm
    sorted_jns = sorted( jns.keys() )
    start_i = 0
    merge_jns = []
    for bin_i, bin in enumerate( gene_bndry_bins ):
        # increment the start pointer
        new_start_i = start_i
        for jn_start, jn_stop in sorted_jns[start_i:]:
            if jn_stop >= bin.start:
                break
            new_start_i += 1
        
        start_i = new_start_i
        
        # find matching junctions
        for jn_start, jn_stop in sorted_jns[start_i:]:
            if jn_start > bin.stop:
                break
            if jn_stop > bin.stop:
                merge_jns.append( ( (jn_start, jn_stop), bin_i ) )
    
    genes_graph = graph()
    genes_graph.add_nodes( xrange( len( gene_bndry_bins ) ) )
    
    for jn, bin_i in merge_jns:
        # find the bin index that the jn merges into
        for end_bin_i, bin in enumerate( gene_bndry_bins[bin_i:] ):
            if jn[1]+1 < bin.start:
                break

        assert bin_i + end_bin_i < len( gene_bndry_bins )
        for i in xrange( bin_i+1, bin_i+end_bin_i ):
            try:
                genes_graph.add_edge((bin_i, i))
            except pygraph.classes.exceptions.AdditionError:
                pass

    conn_nodes = connected_components( genes_graph )
    connected_bins = [ [] for i in xrange(max(conn_nodes.values())) ]
    for start, stop in conn_nodes.iteritems():
        connected_bins[stop-1].append( start )

    new_bins = []
    for bins in connected_bins:
        start_bin = gene_bndry_bins[ bins[0] ]
        stop_bin = gene_bndry_bins[ bins[-1] ]
        new_bins.append( 
            Bin( start_bin.start, stop_bin.stop, 
                 start_bin.left_label, stop_bin.right_label, "GENE")
        )
    
    # finally, find cage peaks to refine the gene boundaries
    # find cage peaks
    refined_gene_bndry_bins = Bins( chrm, strand, [] )
    cage_peaks = Bins( chrm, strand )
    for gene_bin in new_bins:
        gene_cage_peaks = find_cage_peaks_in_gene( 
                (chrm, strand), gene_bin, cage_cov, rnaseq_cov )
        cage_peaks.extend( gene_cage_peaks )
        # refine the gene boundaries, now that we know where the promoters are
        if len( gene_cage_peaks ) > 0:
            refined_gene_bndry_bins.append( 
                Bin( min( peak.start for peak in gene_cage_peaks ), 
                     gene_bin.stop, 
                     gene_bin.left_label, gene_bin.right_label, "GENE") )
        else:
            refined_gene_bndry_bins.append( 
                Bin( gene_bin.start, gene_bin.stop, 
                     gene_bin.left_label, gene_bin.right_label, "GENE") )

    return refined_gene_bndry_bins

def find_cage_peaks_in_gene( ( chrm, strand ), gene, cage_cov, rnaseq_cov ):
     raw_peaks = find_peaks( cage_cov[gene.start:gene.stop+1], 
                             window_len=20, min_score=MIN_NUM_CAGE_TAGS,
                             max_score_frac=MAX_CAGE_FRAC, max_num_peaks=20 )
     if len( raw_peaks ) == 0:
         return []
     
     cage_peaks = Bins( chrm, strand )
     for peak_st, peak_sp in raw_peaks:
         cage_peaks.append( Bin( peak_st+gene.start, peak_sp+gene.start+1,
                                 "CAGE_PEAK_START", "CAGE_PEAK_STOP", "CAGE_PEAK") )
     return cage_peaks

def find_peaks( cov, window_len, min_score, max_score_frac, max_num_peaks ):
    cumsum_cvg_array = \
        numpy.append(0, numpy.cumsum( cov ))
    scores = cumsum_cvg_array[window_len:] - cumsum_cvg_array[:-window_len]
    indices = numpy.argsort( scores )
    
    def overlaps_prev_peak( new_loc ):
        for start, stop in peaks:
            if not( new_loc > stop or new_loc + window_len < start ):
                return True
        return False
    
    # merge the peaks
    def grow_peak( start, stop, grow_size=max(3, window_len/4), min_grow_ratio=0.5 ):
        # grow a peak at most max_num_peaks times
        for i in xrange(max_num_peaks):
            curr_signal = cov[start:stop+1].sum()
            if curr_signal < min_score:
                return ( start, stop )
            
            downstream_sig = cov[max(0, start-grow_size):start].sum()
            upstream_sig = cov[stop+1:stop+1+grow_size].sum()
            exp_factor = float( stop - start + 1 )/grow_size
            
            # if neither passes the threshold, then return the current peak
            if float(max( upstream_sig, downstream_sig ))*exp_factor \
                    < curr_signal*min_grow_ratio: return (start, stop)
            
            # otherwise, we know one does
            if upstream_sig > downstream_sig:
                stop += grow_size
            else:
                start = max(0, start - grow_size )
        
        if VERBOSE:
            print "Warning: reached max peak iteration at %i-%i ( signal %.2f )"\
                % (start, stop, cov[start:stop+1].sum() )
            print 
        return (start, stop )
    
    peaks = []
    peak_scores = []
    
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
        merged_peaks = set()
        new_peaks = []
        new_scores = []
        for pk_i, (peak, score) in enumerate(peaks_and_scores):
            if pk_i in merged_peaks: continue
            curr_pk = list( peak )
            curr_score = score
            for i_pk_i, (i_peak, i_score) in enumerate(peaks_and_scores):
                if i_pk_i in merged_peaks: continue
                if i_peak[0] < curr_pk[0]: continue
                if i_peak[0] - curr_pk[1] < max( window_len, 
                                                 curr_pk[1]-curr_pk[0] ):
                    curr_pk[1] = i_peak[1]
                    curr_score += i_score
                    merged_peaks.add( i_pk_i )
                else:
                    break

            new_peaks.append( curr_pk )
            new_scores.append( curr_score )
        return zip( new_peaks, new_scores )
    
    peaks_and_scores = sorted( zip(peaks, peak_scores) )
    old_len = len( peaks_and_scores )
    for i in xrange( 99 ):
        if i == 100: assert False
        peaks_and_scores = merge_peaks( peaks_and_scores )
        if len( peaks_and_scores ) == old_len: break
    
    max_score = max( s for p, s in peaks_and_scores )
    return [ pk for pk, score in peaks_and_scores \
                 if score/max_score > max_score_frac ]


def find_left_exon_extensions( start_index, start_bin, gene_bins, rnaseq_cov ):
    internal_exons = []
    ee_indices = []
    start_bin_cvg = start_bin.mean_cov( rnaseq_cov )
    for i in xrange( start_index-1, 0, -1 ):
        bin = gene_bins[i]

        # break at canonical exons
        if bin.left_label == 'R_JN' and bin.right_label == 'D_JN':
            break
        
        # make sure the average coverage is high enough
        bin_cvg = rnaseq_cov[bin.start:bin.stop].mean()

        if bin_cvg < MIN_EXON_BPKM:
            break
        
        if bin.stop - bin.start > 20 and \
                start_bin_cvg/(bin_cvg+1) >EXON_EXT_CVG_RATIO_THRESH:
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

def find_right_exon_extensions( start_index, start_bin, gene_bins, rnaseq_cov ):
    exons = []
    ee_indices = []
    start_bin_cvg = start_bin.mean_cov( rnaseq_cov )
    for i in xrange( start_index+1, len(gene_bins) ):
        bin = gene_bins[i]

        # if we've reached a canonical exon, break
        if bin.left_label == 'R_JN' and bin.right_label == 'D_JN':
            break
        
        # make sure the average coverage is high enough
        bin_cvg = rnaseq_cov[bin.start:bin.stop].mean()
        if bin_cvg < MIN_EXON_BPKM:
            break
        
        if bin.stop - bin.start > 20 and \
                start_bin_cvg/(bin_cvg+1) >EXON_EXT_CVG_RATIO_THRESH:
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

def find_internal_exons_from_pseudo_exons( pseudo_exons ):
    """Find all of the possible exons from the set of pseudo exons.
    
    """
    internal_exons = []
    # condition on the number of pseudo exons
    for exon_len in xrange(1, len(pseudo_exons)+1):
        for start in xrange(len(pseudo_exons)-exon_len+1):
            start_ps_exon = pseudo_exons[start]
            stop_ps_exon = pseudo_exons[start+exon_len-1]

            if stop_ps_exon.right_label != 'D_JN': continue

            if start_ps_exon.left_label != 'R_JN': continue

            # each potential exon must have a canonical exon in it
            if not any ( exon.type == 'EXON' 
                         for exon in pseudo_exons[start:start+exon_len] ):
                continue
            internal_exons.append( Bin(start_ps_exon.start, stop_ps_exon.stop,
                                       start_ps_exon.left_label, 
                                       stop_ps_exon.right_label,
                                       'EXON') )
    
    internal_exons = sorted( internal_exons )
    
    return internal_exons

def find_tss_exons_from_pseudo_exons( tss_exon, pseudo_exons ):
    tss_exons = []
    # find the first pseudo exon that the tss exon connects to
    for i, pse in enumerate( pseudo_exons ):
        if pse.start == tss_exon.stop: 
            break
    
    for pse in pseudo_exons[i:]:
        tss_exons.append( Bin(tss_exon.start, pse.stop,
                              tss_exon.left_label, pse.right_label, "TSS_EXON"))
    
    return tss_exons

def find_pseudo_exons_in_gene( ( chrm, strand ), gene, rnaseq_cov, cage_cov, polya_sites, jns ):
    # filter the jns
    max_jn_cnt = 0
    donor_cnts = defaultdict( int )
    receiver_cnts = defaultdict( int )
    
    for start, stop, cnt in jns:
        if cnt == 0: continue
        # if this is not in the gene, filter it 
        if start < gene.start or stop > gene.stop:
            continue
        max_jn_cnt = max( max_jn_cnt, cnt )
        donor_cnts[ start ] = max( cnt, donor_cnts[ start ] )
        receiver_cnts[ stop ] = max( cnt, receiver_cnts[ stop ] )

    jns =  [ (start, stop, cnt) for (start, stop, cnt) in jns \
             if (start >= gene.start and stop <= gene.stop) ]    
    jns_hash = dict( ((start, stop), cnt) for (start, stop, cnt) in jns )
    
    pseudo_exons = []
    polya_sites = [ polya for polya in polya_sites 
                    if not (polya <= gene.start or polya > gene.stop) ]
    cage_peaks = find_cage_peaks_in_gene( 
        ( chrm, strand ), gene, cage_cov, rnaseq_cov )
    
    locs = {}
    for polya in polya_sites:
        locs[ polya ] = "POLYA"
    
    for start, stop, cnt in jns:
        locs[start-1] = "D_JN"
        locs[stop+1] = "R_JN"

    for start, stop in find_empty_regions( rnaseq_cov[gene.start:gene.stop+1] ):
        if stop - start < MIN_EMPTY_REGION_LEN: continue
        if start in locs or stop in locs:
            continue
        locs[start+gene.start] = "ESTART"
        locs[stop+gene.start] = "ESTOP"
    
    # build all of the bins
    poss = sorted( locs.iteritems() )
    poss = merge_empty_labels( poss )
    if len( poss ) == 0:
        return [], [], [], []

    gene_bins = []
    if gene.start < poss[0][0]:
         gene_bins.append( Bin(gene.start, poss[0][0], "TSS", poss[0][1]) )
    for index, ((start, left_label), (stop, right_label)) in \
            enumerate(izip(poss[:-1], poss[1:])):
        gene_bins.append( Bin(start, stop, left_label, right_label) )

    if gene.stop != poss[-1][0]:
        gene_bins.append( Bin(poss[-1][0], gene.stop, poss[-1][1], "TES") )
    
    # find tss exons
        # overlaps a cage peak, then 
    # find exon starts pseudo exons ( spliced_to, ... )
    canonical_exon_starts = [ i for i, bin in enumerate( gene_bins )
                              if bin.left_label == 'R_JN' 
                              and bin.right_label == 'D_JN' ]
    for ce_i in canonical_exon_starts:
        canonical_bin = gene_bins[ce_i]
        if canonical_bin.mean_cov(rnaseq_cov) < MIN_EXON_BPKM:
            continue
        
        pseudo_exons.append( Bin( canonical_bin.start, canonical_bin.stop,
                                    canonical_bin.left_label, 
                                    canonical_bin.right_label, 
                                    "EXON"  ) )
        
        pseudo_exons.extend( find_left_exon_extensions(
                ce_i, canonical_bin, gene_bins, rnaseq_cov))
        
        pseudo_exons.extend( find_right_exon_extensions(
                ce_i, canonical_bin, gene_bins, rnaseq_cov))
    
    cage_peak_bin_indices = []
    tss_exons = []
    for peak in cage_peaks:
        # find the first donor junction right of the cage peaks
        for bin_i, bin in enumerate(gene_bins):
            if bin.stop < peak.stop: continue
            if bin.right_label in ('D_JN', 'POLYA', 'ESTART') : break
        
        cage_peak_bin_indices.append( bin_i )
        tss_exons.append( Bin( peak.start, bin.stop,
                               "CAGE_PEAK", 
                               bin.right_label, 
                               "TSS_EXON"  ) )

    for cage_peak_i in cage_peak_bin_indices:
        bin = gene_bins[cage_peak_i]
        pseudo_exons.extend( find_right_exon_extensions(
                cage_peak_i, bin, gene_bins, rnaseq_cov))
    
    # find tes exons
    tes_exon_indices = [ i for i, bin in enumerate( gene_bins )
                         if bin.right_label in ('POLYA', )
                         and bin.start < gene.stop ]

    tes_exons = []
    for tes_exon_i in tes_exon_indices:
        bin = gene_bins[tes_exon_i]
        
        tes_exons.append( Bin( bin.start, bin.stop,
                               bin.left_label, 
                               bin.right_label, 
                               "TES_EXON"  ) )
        
        pseudo_exons.append( tes_exons[-1] )
        pseudo_exons.extend( find_left_exon_extensions(
                tes_exon_i, bin, gene_bins, rnaseq_cov ))

    # build exons from the pseudo exon set
    pseudo_exons = list( set( pseudo_exons ) )
    pseudo_exons.sort( key=lambda x: (x.start, x.stop ) )
    
    return cage_peaks, tss_exons, pseudo_exons, tes_exons

def find_exons_in_gene( ( chrm, strand ), gene, 
                        rnaseq_cov, cage_cov, polya_sites, jns ):
    cage_peaks, tss_pseudo_exons, pseudo_exons, tes_pseudo_exons = \
        find_pseudo_exons_in_gene(
            ( chrm, strand ), gene, rnaseq_cov, cage_cov, polya_sites, jns )
    
    # find the contiguous set of adjoining exons
    slices = [[0,],]
    for i, ( start_ps_exon, stop_ps_exon ) in enumerate( 
            zip(pseudo_exons[:-1], pseudo_exons[1:]) ):
        # if there is a gap...
        if start_ps_exon.stop < stop_ps_exon.start:
            slices[-1].append(i+1)
            slices.append([i+1,])
    
    # add in the last segment
    if slices[-1][0] < len( pseudo_exons ):
        slices[-1].append( len(pseudo_exons) )
    else:
        slices.pop()
    
    gpd_pseudo_exons = []    
    for start, stop in slices:
        gpd_pseudo_exons.append( pseudo_exons[start:stop] )
        
    if len( gpd_pseudo_exons ) == 0:
        print "============================================= NO EXONS IN REGION"
        print gene
        return list(chain(tss_pseudo_exons, pseudo_exons, tes_pseudo_exons)), \
            [], [], [], [], []


    internal_exons = []
    for pseudo_exons_grp in gpd_pseudo_exons:
        internal_exons.extend( 
            find_internal_exons_from_pseudo_exons( pseudo_exons_grp ) )

    tss_exons = []
    for tss_exon in tss_pseudo_exons:
        if tss_exon.right_label in 'D_JN':
            tss_exons.append( tss_exon )
        elif tss_exon.right_label == 'POLYA':
            tss_exons.append( tss_exon )
        else:
            pass
            #try:
            #    assert tss_exon.stop >= gpd_pseudo_exons[0][0].start
            #except:
            #    print tss_exon
            #    print gpd_pseudo_exons[0][0]
            #    raise
        
        for pe_grp in gpd_pseudo_exons:
            if pe_grp[0].start > tss_exon.stop:  
                continue
            if pe_grp[-1].stop <= tss_exon.start:
                break
            
            tss_exons.extend( find_tss_exons_from_pseudo_exons( 
                    tss_exon, pe_grp ) )

    single_exon_genes = set()
    for exon in tss_exons:
        if exon.right_label == 'POLYA':
            se_gene = copy(exon)
            se_gene.type = 'SE_GENE'
            single_exon_genes.add( se_gene )
    
    tes_exons = []
    for tes_exon in tes_pseudo_exons:
        if tes_exon.left_label == 'R_JN':
            tes_exons.append( tes_exon )
        elif tes_exon.left_label == 'POLYA':
            pass
        else:
            assert tes_exon.start <= gpd_pseudo_exons[-1][-1].stop
        
        # find the overlapping intervals
        for pe_grp in gpd_pseudo_exons:
            if pe_grp[0].start >= tes_exon.stop:  
                break
            if pe_grp[-1].stop <= tes_exon.start:
                continue
            if tes_exon == pe_grp[0]:
                continue
            
            # now we know that the tes exon overlaps the pseudo exon grp
            #print tes_exon, [ (pe.left_label, pe.right_label) for pe in pe_grp ]
            #print pe_grp
            
            # find the psuedo exon that shares a boundary
            last_pe_i = max( i for i, pe in enumerate( pe_grp ) 
                             if pe.stop == tes_exon.start )

            for pe in pe_grp[:last_pe_i+1]:
                # we had this earlier, buyt I think we want tes exons to
                # extend into real exons ( although this gives us fragments )
                #if pe.left_label != 'R_JN':
                #    continue

                tes_exons.append( 
                    Bin( pe.start, tes_exon.stop, pe.left_label, 
                         tes_exon.right_label, "TES_EXON" )
                    )
        
    all_exons = chain( tss_pseudo_exons, pseudo_exons, 
                       tes_pseudo_exons, single_exon_genes)
    return cage_peaks, all_exons, \
        tss_exons, internal_exons, tes_exons, single_exon_genes
        


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
    lower = stats.stats.scoreatpercentile(Q, 10)
    upper = stats.stats.scoreatpercentile(Q, 90)
    return lower, upper

def get_qrange_short( np ):
    L = len(np)
    return np.min(), np.max()

def filter_exon( exon, wig, min_avg_cvg=0.01, 
                 min_short_cvg=1.0, short_exon_length=400,
                 min_long_cvg=10 ):
    '''Find all the exons that are sufficiently homogenous and expressed.
    
    '''
    start = exon.start
    end = exon.stop
    vals = wig[start:end+1]
    mean_cvg = vals.mean()
    
    # if virtually off, forget it
    if mean_cvg < min_avg_cvg: 
        return True
    
    # if its short, just make sure it's covered to at least an average of 1X 
    if end-start < short_exon_length: 
        if mean_cvg > min_short_cvg:
            return False
        else:
            return True
    
    # get the interquartile range
    low, high = get_qrange_long( vals, 50, 300 ) 
    # if the lower quartile is 0, ditch it
    if low == 0 or high < min_long_cvg: 
        return True
    
    IQ_ratio = high / max(low, min_avg_cvg)
    # if the IQ range is < 100, chalk it up to inhomogeneity and accept it
    if IQ_ratio < 100:
        return False
    
    # if the IQ range is "boarder line", but the low is high, keep it, what the hell
    if IQ_ratio >= 100 and IQ_ratio < 500 and low > 50:
        return False
    
    return True

def filter_exons( exons, rnaseq_cov ):
    for exon in exons:
        if not filter_exon( exon, rnaseq_cov ):
            yield exon
    
    return

def filter_tes_exons(tes_exons,rnaseq_cov,global_thresh=0.1,ratio_thresh=0.01):    
    scores = []
    mx_mean = 0
    mx_lower = 0
    mx_upper = 0
    for tes_exon in tes_exons:
        start = tes_exon.start
        end = tes_exon.stop
        curr_wig = rnaseq_cov[start:end+1]
        L = end-start+1
        step = 10
        window = 20
        if L > 500:
            step = 50
            window = 300
        elif L > 300:
            step = 30
            window = 100
        elif L > 200:
            step = 20
            window = 50
        lower, upper = get_qrange_long( curr_wig, step, window )
        M = curr_wig.mean()
        scores.append( [M, lower, upper]  )
        if M > mx_mean: mx_mean = M
        if lower > mx_lower: mx_lower = lower
        if upper > mx_upper: mx_upper = upper
    
    if mx_mean < global_thresh: 
        return

    
    for tes_exon, score in zip( tes_exons, scores ):
        if score[0]/mx_mean < ratio_thresh: continue
        if mx_lower > 0:
            if score[1]/mx_lower < ratio_thresh: continue
        else:
            pass # note that this could mean there are no good 3UTRs at this locus
        if score[2]/(mx_upper+1e-6) < ratio_thresh: 
            continue
        
        yield tes_exon
        
    return

def find_exons_in_contig( ( chrm, strand ), 
                          rnaseq_cov, jns, cage_cov, polya_sites ):
    if strand == '-':
        rnaseq_cov, jns, cage_cov, polya_sites = reverse_contig_data( 
            rnaseq_cov, jns, cage_cov, polya_sites )
        
    # find gene regions
    # first, find the gene bin indices
    gene_bndry_bins = find_gene_boundaries( 
        (chrm, strand), cage_cov, rnaseq_cov, polya_sites, jns )
    
    if WRITE_DEBUG_DATA:
        gene_bndry_bins.writeBed( 
            debug_fps['geneBoundariesFps'][strand], len(rnaseq_cov) )
    
    
    # find all exons
    cage_peaks = Bins( chrm, strand, [] )
    ps_exons = Bins( chrm, strand, [] )
    tss_exons = Bins( chrm, strand, [] )
    internal_exons = Bins( chrm, strand, [] )
    tes_exons = Bins( chrm, strand, [] )
    se_genes = Bins( chrm, strand, [] )
    
    
    sorted_jns = sorted( jns.iteritems() )
    jn_starts = [ i[0][0] for i in sorted_jns ]
    jn_stops = [ i[0][1] for i in sorted_jns ]
    jn_values = [ i[1] for i in sorted_jns ]
    
    for gene in gene_bndry_bins:
        # find the junctions associated with this gene
        gj_sa = bisect( jn_stops, gene.start )
        gj_so = bisect( jn_starts, gene.stop )
        gene_jns = zip( jn_starts[gj_sa:gj_so], 
                        jn_stops[gj_sa:gj_so], 
                        jn_values[gj_sa:gj_so] )
        
        # find the polyas associated with this gene
        gene_polya_sa_i = polya_sites.searchsorted( gene.start )
        gene_polya_so_i = polya_sites.searchsorted( gene.stop, side='right' )
        gene_polyas = polya_sites[gene_polya_sa_i:gene_polya_so_i]
        
        gene_cage_peaks, gene_ps_exons, gene_tss_exons, \
            gene_internal_exons, gene_tes_exons, gene_se_genes = \
            find_exons_in_gene( ( chrm, strand ), gene, 
                                rnaseq_cov, cage_cov, polya_sites, gene_jns )
        
        cage_peaks.extend( gene_cage_peaks )
        ps_exons.extend( gene_ps_exons )
        internal_exons.extend( filter_exons( gene_internal_exons, rnaseq_cov ) )
        tss_exons.extend( filter_exons( gene_tss_exons, rnaseq_cov ) )
        tes_exons.extend( filter_exons( gene_tes_exons, rnaseq_cov ) )
        se_genes.extend( filter_exons( gene_se_genes, rnaseq_cov ) )
    
    tss_exon_starts = set( x.start for x in tss_exons )
    filtered_cage_peaks = Bins( cage_peaks.chrm, cage_peaks.strand, 
                                ( x for x in cage_peaks 
                                  if x.start in tss_exon_starts ) )
    
    all_exons = Bins( chrm, strand, 
                      chain( filtered_cage_peaks, 
                             tss_exons, internal_exons, tes_exons, se_genes))
    
    if WRITE_DEBUG_DATA:
        all_exons.writeBed( debug_fps['allExonsFps'][strand], len(rnaseq_cov) )
        ps_exons.writeBed( debug_fps['psExonsFps'][strand], len(rnaseq_cov) )
    
    return all_exons


def init_debug_fps( out_file_prefix ):
    debug_ofps = {}
    
    binsFps = { "+": ThreadSafeFile( 
            out_file_prefix + ".bins.plus.bed", "w", "bins_plus" ),
                "-": ThreadSafeFile( 
            out_file_prefix + ".bins.minus.bed", "w", "bins_minus" ) }
    debug_ofps['binsFps'] = binsFps

    geneBoundariesFps = { 
        "+": ThreadSafeFile( 
            out_file_prefix + ".gene_boundaries.plus.bed", "w", "gene_bndrys_plus" ),
        "-": ThreadSafeFile( 
            out_file_prefix + ".gene_boundaries.minus.bed", "w", "gene_bndrys_minus" ) 
    }
    debug_ofps['geneBoundariesFps'] = geneBoundariesFps

    allExonsFps = { 
        "+": ThreadSafeFile( 
            out_file_prefix + ".all_exons.plus.bed", "w", "all_exons_plus" ),
        "-": ThreadSafeFile(
            out_file_prefix + ".all_exons.minus.bed", "w", "all_exons_minus" ) 
    }
    debug_ofps['allExonsFps'] = allExonsFps

    psExonsFps = { 
        "+": ThreadSafeFile( 
            out_file_prefix + ".pseudo_exons.plus.bed", "w", "pseudo_exons_plus" ),
        "-": ThreadSafeFile(
            out_file_prefix + ".pseudo_exons.minus.bed", "w", "pseudo_exons_minus" ) 
    }
    debug_ofps['psExonsFps'] = psExonsFps
    
    return debug_ofps

def parse_arguments():
    import argparse

    parser = argparse.ArgumentParser(\
        description='Find exons from wiggle and junctions files.')

    parser.add_argument( 'junctions', type=file, \
        help='GTF format file of junctions(introns).')
    parser.add_argument( 'chrm_sizes_fname', type=file, \
        help='File with chromosome names and sizes.')

    parser.add_argument( 'wigs', type=file, nargs="+", \
        help='wig files over which to search for exons.')
    
    parser.add_argument( '--cage-wigs', type=file, nargs='+', \
        help='wig files with cage reads, to identify tss exons.')
    parser.add_argument( '--polya-candidate-sites', type=file, nargs='*', \
        help='files with allowed polya sites.')
    
    parser.add_argument( '--out-filename', '-o', 
                         default="discovered_elements.bed",\
        help='Output file name. (default: discovered_elements.bed)')
    
    parser.add_argument( '--verbose', '-v', default=False, action='store_true',\
        help='Whether or not to print status information.')
    parser.add_argument('--write-debug-data',default=False,action='store_true',\
        help='Whether or not to print out gff files containing intermediate exon assembly data.')
    parser.add_argument( '--threads', '-t', default=1, type=int,
        help='The number of threads to use.')
        
    args = parser.parse_args()

    global num_threads
    num_threads = args.threads
        
    # prepare the intermediate output objects
    global WRITE_DEBUG_DATA
    WRITE_DEBUG_DATA = args.write_debug_data
    if WRITE_DEBUG_DATA:
        global debug_fps
        debug_fps = init_debug_fps( args.out_filename )
    
    # set flag args
    global VERBOSE
    VERBOSE = args.verbose
    
    rd1_plus_wigs = [ fp for fp in args.wigs 
                      if fp.name.endswith("rd1.plus.bedGraph") ]
    rd1_minus_wigs = [ fp for fp in args.wigs 
                      if fp.name.endswith("rd1.minus.bedGraph") ]
    rd2_plus_wigs = [ fp for fp in args.wigs 
                      if fp.name.endswith("rd2.plus.bedGraph") ]
    rd2_minus_wigs = [ fp for fp in args.wigs 
                      if fp.name.endswith("rd2.minus.bedGraph") ]
    
    rnaseq_grpd_wigs = [ rd1_plus_wigs, rd1_minus_wigs, rd2_plus_wigs, rd2_minus_wigs ]
        
    cage_plus_wigs = [ fp for fp in args.cage_wigs 
                      if fp.name.lower().endswith("+.bedgraph")
                       or fp.name.lower().endswith("plus.bedgraph")
                       or fp.name.lower().endswith("+.wig")
                       or fp.name.lower().endswith("plus.wig")]

    cage_minus_wigs = [ fp for fp in args.cage_wigs 
                      if fp.name.lower().endswith("-.bedgraph")
                        or fp.name.lower().endswith("minus.bedgraph")
                        or fp.name.lower().endswith("-.wig")
                        or fp.name.lower().endswith("minus.wig") ]

    cage_grpd_wigs = [ cage_plus_wigs, cage_minus_wigs ]
    
    ofp = open( args.out_filename, "w" )
    
    return rnaseq_grpd_wigs, args.junctions, args.chrm_sizes_fname, \
        cage_grpd_wigs, args.polya_candidate_sites, ofp


def main():
    wigs, jns_fp, chrm_sizes_fp, cage_wigs, polya_candidate_sites_fps, ofp \
        = parse_arguments()
    
    if VERBOSE: print >> sys.stderr,  'Loading merged read pair wiggles'    
    fps =[wigs[0][0], wigs[1][0], wigs[2][0], wigs[3][0]]
    strands = ["+", "-", "+", "-"]
    read_cov = Wiggle( chrm_sizes_fp, fps, strands )
    if VERBOSE: print >> sys.stderr, 'Finished loading merged read pair wiggles'
    
    if VERBOSE: print >> sys.stderr,  'Loading CAGE.'
    all_cage_wigs, cage_strands = [], []
    all_cage_wigs.extend( cage_wigs[0] )
    cage_strands.extend( ['+']*len( cage_wigs[0] ) )
    all_cage_wigs.extend( cage_wigs[1] )
    cage_strands.extend( ['-']*len( cage_wigs[1] ) )
    cage_cov = Wiggle( chrm_sizes_fp, all_cage_wigs, cage_strands )
    for cage_fps in cage_wigs: [ cage_fp.close() for cage_fp in cage_fps ]
    if VERBOSE: print >> sys.stderr, 'Finished loading CAGE data'
    
    if VERBOSE: print >> sys.stderr,  'Loading candidate polyA sites'
    polya_sites = find_polya_sites([x.name for x in polya_candidate_sites_fps])
    for fp in polya_candidate_sites_fps: fp.close()
    if VERBOSE: print >> sys.stderr, 'Finished loading candidate polyA sites'

    if VERBOSE: print >> sys.stderr,  'Loading junctions.'
    jns = Junctions( parse_jn_gff( jns_fp.name ) )
        
    all_cage_peaks = []
    single_exon_genes = []
    tss_exons = []
    internal_exons = []
    tes_exons = []

    ofp.write('track name="discovered_elements" visibility=2 itemRgb="On"\n')
    
    keys = sorted( set( jns.keys() ) )
    for chrm, strand in keys:        
        if VERBOSE: print >> sys.stderr, \
                'Processing chromosome %s strand %s.' % ( chrm, strand )
        
        contig_len = len(read_cov[(chrm, strand)])
        
        all_elements = find_exons_in_contig( \
           ( chrm, strand ),
           read_cov[ (chrm, strand) ].asarray(),
           jns[ (chrm, strand) ],
           cage_cov[ (chrm, strand) ].asarray(),
           polya_sites[ (chrm, strand) ] )
        
        for jn, cnt in jns[(chrm, strand)].iteritems():
            bin = Bin(jn[0], jn[1], 'donor', 'acceptor', 'INTRON', cnt)
            if strand == '-':
                bin = bin.reverse_strand(contig_len)
            all_elements.append( bin )
                
        
        write_unified_bed( all_elements, contig_len, ofp )
    
    if WRITE_DEBUG_DATA:
        for fps_dict in debug_fps.values():
            for fp in fps_dict.values():
                fp.close()
    
    return
        
if __name__ == "__main__":
    main()