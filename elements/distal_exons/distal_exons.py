import sys
import os
import numpy
from collections import defaultdict
from operator import itemgetter

VERBOSE = False

# add parent(slide) directory to sys.path and import SLIDE mods
sys.path.append( os.path.join( os.path.dirname(__file__), 
                               "..", "..", "sparsify" ) )
from gene_models import parse_junctions_file, parse_gff_line, GenomicInterval

sys.path.append( os.path.join( os.path.dirname(__file__), 
                               "..", "..", "file_types" ) )
from wiggle import Wiggle

def convert_to_genomic_intervals( all_exons ):
    converted_exons = set()
    for (chrm, strand), exons in all_exons.iteritems():
        for start, stop in exons:
            converted_exons.add( \
                GenomicInterval( chrm, strand, start, stop ) )
    
    return converted_exons

def find_nonoverlapping_signal( exons, read_cvg, chrm, strand ):
    # concatinate the smmothed cage signal in all of the exons
    cvg_arrays = []
    nonoverlapping_signal = numpy.zeros(0)
    # we keep track of the previous stop position to 
    # make sure that we don't double count overlapping exons.
    # Since the starts are sorted, the only way the next exon
    # can overlap is if the stop of the previous exon is greater 
    # than the start of the next exon. So, we simply take the max of
    # the two, with a special case in case the exons are sub-sumed. 
    prev_stop = -1
    for start, stop in sorted(exons):
        cvg_arrays.append( \
            read_cvg[(chrm, strand)][ start:stop+1 ])
        # if there is actually some new sequence, add it
        if stop > prev_stop:
            nonoverlapping_signal = numpy.append(
                nonoverlapping_signal,
                read_cvg[(chrm, strand)][ \
                    max( start, prev_stop ):stop+1 ]
            )
        
        prev_stop = max( stop, prev_stop )

    return nonoverlapping_signal, cvg_arrays

def filter_exons_by_quantiles( exons, cvg, smooth_read_cvg, threshold_frac, chrm, strand ):
    """The approach is as follows:

    1) Find promoter exons:
    - smooth distal ( CAGE, polya ) signal with SMOOTH_SIZE bp smoothing window
    - order smoothed arrays by size
    - threshold every position in the lower RATIO_THRESH% ( by *signal* )
    - keep exons that still have cage signal
    
    """
    exons = sorted( exons )
    
    nonoverlapping_signal, cvg_arrays = \
        find_nonoverlapping_signal( exons, smooth_read_cvg, chrm, strand )
    nonoverlapping_signal.sort()
    # if there isno signal, then return every exon
    if nonoverlapping_signal[-1] == 0: 
        raise ValueError, "No signal."
    
    thresh = threshold_frac*nonoverlapping_signal.sum()
    
    # find how many bases we need to observe to account for at least
    # THRESHOLD_FRAC of the total read coverage
    tot = 0
    for thresh_val in reversed(nonoverlapping_signal):
        tot += thresh_val
        if tot > thresh: 
            break

    # threshold bases that are below the threshold in each exon
    new_exons = []
    for i, array in enumerate( cvg_arrays ):
        # get the indices to threshold
        bad_indices = ( array < thresh_val ).nonzero()[0]
        exon_start = exons[i][0]
        shifted_bad_indices = exon_start + bad_indices
        if len( shifted_bad_indices ) < len( array ):
            new_exons.append( exons[i] )
    
    return new_exons

def filter_exons_by_max_read_coverage( \
        exons, cvg_array, window_len, min_cvg_frac ):
    """Remove exons which contain less than MIN_SCORE_RATIO distal read coverage
    """
    def get_max_region_cvg( exon, window_len ):
        start, stop = exon
        
        # get maximal tss coverage region accoss exon
        cumsum_cvg_array = \
            numpy.append(0, numpy.cumsum( cvg_array[start : stop + 1]))
        
        # if the region is too short, return the total for the entire region
        # but scale so that the average is correct
        if len( cumsum_cvg_array ) <= window_len:
            return cumsum_cvg_array[-1]*( \
                float( window_len )/len( cumsum_cvg_array ) )
        
        # get the coverage total for every window in the interval of length
        # window_len
        score = ( cumsum_cvg_array[window_len:] \
                      - cumsum_cvg_array[:-window_len] ).max()
        return score
    
    def get_exon_scores( exons, window_len ):
        max_score = 0
        exons_w_scores = []
        for exon in exons:
            score = get_max_region_cvg( exon, window_len )
            max_score = max( max_score, score )
            exons_w_scores.append( (exon, score) )
        
        return exons_w_scores, max_score
    
    def get_candidate_exons( exons_w_scores, max_score ):
        """remove exons with sufficiently low distal read coverage
        """
        # the non_inclusive minimum score is scaled to the 
        # ceiling of the max_score * MIN_SCORE_RATIO
        # i.e. if the min_cvg_frac is 0.01 and the max score is 30
        # a score greater than one will suffice for *high score*
        min_score = int( min_cvg_frac * max_score ) + 1
        
        high_score_exons = []
        for exon, score in exons_w_scores:
            if score > min_score:
                high_score_exons.append( exon )
        
        return high_score_exons
    
    
    exons_w_scores, max_score = get_exon_scores( exons, window_len )
    if max_score == 0:
        raise ValueError, "No signal."
    
    candidate_exons = get_candidate_exons( exons_w_scores, max_score )
    
    return candidate_exons

def get_unique_internal_splice_exons( exons, strand, is_tss ):
    # get index of internal and external exons
    if (strand == '+' and is_tss) or (strand == '-' and not is_tss):
        int_index = 1
    else:
        int_index = 0
    ext_index = 0 if int_index == 1 else 1
    
    unique_largest_exons = {}
    # get the largest exon at each internal splice site
    for exon in exons:
        int_coord = exon[int_index]
        ext_coord = exon[ext_index]
        if int_coord in unique_largest_exons:
            if (ext_coord < unique_largest_exons[int_coord] \
                    and int_index == 1) \
                    or (ext_coord > unique_largest_exons[int_coord] \
                            and int_index == 0):
                unique_largest_exons[ int_coord ] = ext_coord
        else:
            unique_largest_exons[ int_coord ] = ext_coord
    
    # get exon tuple structure back
    unique_exons = []
    for int_coord, ext_coord in unique_largest_exons.iteritems():
        unique_exons.append( (min(int_coord, ext_coord), \
                                  max(int_coord, ext_coord) ) )
    
    sorted_unique_exons = sorted( unique_exons )
    
    return sorted_unique_exons

def refine_exon_bndrys( exons, all_cvrg, key, is_tss, cvrg_fraction ):
    """Refine distal exons according to distal reads coverage across each exon
    """
    chrm, strand = key
    refined_exons = []
    for start, stop in exons:
        from_upstream = (strand == '+' and not is_tss) \
            or (strand == '-' and is_tss)
        refined_exon = all_cvrg.get_region_w_fraction_cvrg( \
            key, start, stop, cvrg_fraction, from_upstream )
        if None == refined_exon:
            continue
        refined_exons.append( refined_exon )
    
    return refined_exons

def filter_clustered_exons( exons, cage_read_cvg, smooth_cage_read_cvg,       \
                            region_filter_len, min_cvg_ratio, threshold_frac, \
                            chrm, strand ):
    try:
        filtered_exons_1 = filter_exons_by_max_read_coverage( \
            exons, cage_read_cvg[(chrm, strand)], \
            region_filter_len, min_cvg_ratio )
        
        filtered_exons_2 = filter_exons_by_quantiles( \
            exons, cage_read_cvg, smooth_cage_read_cvg,
            threshold_frac, chrm, strand)
        
        filtered_exons = sorted( set(filtered_exons_1).intersection(\
                filtered_exons_2) )

    except ValueError, inst:
        if str( inst ) != "No signal.":
            raise
        err_str = "WARNING: Region %s %s %s had no CAGE signal." \
            % (chrm, strand, exons)
        print >> sys.stderr, err_str
        return exons
    
    return filtered_exons


def find_distal_exons( clustered_exons, all_cvrg, chrm, strand, \
                           is_tss, cvrg_fraction ):
    """find exons which have distal read coverage and refine their boundaries
    """
    all_exons = []
    for exon_cluster_id, exons in enumerate(clustered_exons):
        # uniquify by internal coordinate
        unique_exons = get_unique_internal_splice_exons( \
            exons, strand, is_tss )
        
        # refine external boundary using distal read signal
        refined_exons = refine_exon_bndrys( \
            unique_exons, all_cvrg, (chrm, strand), is_tss, cvrg_fraction )
        
        all_exons.extend( refined_exons )
    
    return all_exons

def find_all_distal_exons( \
        clustered_exons, all_cvrg, is_tss, cvrg_fraction ):
    """wrapper for find_distal_exons
    """
    all_exons = {}
    # process each chrm, strand combination separately
    keys = sorted( set( clustered_exons ).intersection( all_cvrg ) )
    for (chrm, strand) in keys:
        exons = find_distal_exons(
            clustered_exons[(chrm, strand)], all_cvrg, \
            chrm, strand, is_tss, cvrg_fraction)
        all_exons[(chrm, strand)] = exons
    
    return all_exons

if __name__ == "__main__":
    print "Please use discover_tes_exons_from_polya_reads.py or " + \
        "discover_tss_exons_from_cage_data.py."
