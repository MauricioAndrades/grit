# Copyright (c) 2011-2012 Nathan Boley

import sys, os

from collections import defaultdict
from itertools import izip

sys.path.append( os.path.join( os.path.dirname(__file__), ".." ) )
from files.gtf import load_gtf

VERBOSE = True

FIND_BEST_CONTAIN_MATCH = False
FIND_BEST_JN_MATCH = False

DO_PROFILE = False

def build_test_data():
    """Build test data sets for unit tests
    """
    exons = range( 1, 100, 10 )
    assert len( exons )%2 == 0
    
    ref_trans = []
    t_trans = []
    
    # build 2 identical transcripts
    chrm = "chr_1"
    ref_trans.append( ( chrm, exons) )
    t_trans.append( ( chrm, exons) )
    
    # build 'contains' transcripts
    chrm = "chr_2"
    ref_trans.append( ( chrm, exons[:-2]) )
    t_trans.append( ( chrm, exons) )
    
    chrm = "chr_3"
    ref_trans.append( ( chrm, exons[2:]) )
    t_trans.append( ( chrm, exons) )
    
    chrm = "chr_4"
    ref_trans.append( ( chrm, exons[2:-2]) )
    t_trans.append( ( chrm, exons) )
    
    # build gtf lines from the transcripts
    from merge_transcripts import build_gtf_lines
    def write_gtf( transcripts, ofp ):
        for index, (chrm, trans) in enumerate(transcripts):
            ofp.write(  "\n".join( build_gtf_lines( \
                        chrm, "+", index, index, trans, "" ) ) + "\n" )
    
    with open( "test.ref.gtf", "w" ) as ref_ofp:
        write_gtf( ref_trans, ref_ofp )
    
    with open( "test.t.gtf", "w" ) as t_ofp:
        write_gtf( t_trans, t_ofp )
    
    return

def cluster_overlapping_genes( genes_groups ):
    """
    """
    # build a mapping from gene id to transcripts, and make the list
    # of coverage intervals, grouped by chrm and strand
    genes_mapping = {}
    grpd_boundaries = defaultdict( list )
    
    for source_id, genes in enumerate(genes_groups):
        for gene in genes:
            assert gene.start >= 0
            genes_mapping[ ( source_id, gene.id ) ] = gene.transcripts
            grpd_boundaries[ (gene.chrm, gene.strand) ].append(
                ( (gene.start, gene.stop), (source_id, gene.id) ))
    
    # group all of the genes by overlapping intervals
    clustered_bndries = {}
    for (chrm, strand), boundaries in grpd_boundaries.iteritems():
        boundaries.sort()
        clustered_bndries[(chrm, strand)] = []
        
        curr_grp_max_loc = -1
        for ( min_loc, max_loc ), key in boundaries:
            assert min_loc >= 0
            if min_loc <= curr_grp_max_loc:
                clustered_bndries[(chrm, strand)][-1].append( key )
            else:
                clustered_bndries[(chrm, strand)].append( [ key, ] )
            curr_grp_max_loc = max( max_loc, curr_grp_max_loc )
    
    # join the transcripts to the grouped clusters
    all_clustered_transcripts = {}
    for (chrm, strand), inner_clustered_bndries in clustered_bndries.iteritems():
        clustered_transcripts = []
        for cluster in inner_clustered_bndries:
            clustered_transcripts.append(
                [ [] for i in xrange(len(genes_groups)) ] )
            
            for source_id, gene_id in cluster:
                for transcript in \
                        genes_mapping[ (source_id, gene_id) ]:
                    clustered_transcripts[-1][source_id].append(
                        ( gene_id, transcript.id, transcript ) )
        all_clustered_transcripts[ (chrm, strand) ] = clustered_transcripts
    
    return all_clustered_transcripts

def build_element_stats( ref_genes, t_genes, output_stats ):
    def build_exon_and_intron_sets( genes ):
        internal_exons = set()
        tss_exons = set()
        tes_exons = set()
        all_introns = set()
        single_exon_genes = set()
        for gene in genes:
            for trans in gene.transcripts:
                # skip single exon genes
                if len( trans.exons ) == 1: 
                    single_exon_genes.add( trans.exons[0] )
                    continue
                
                for exon in trans.exons:
                    internal_exons.add( ( gene.chrm, gene.strand, exon ) )
                
                # add the boundary exons
                if gene.strand == '+':
                    tss_exons.add((gene.chrm, gene.strand, trans.exons[0][1] ))
                    tes_exons.add((gene.chrm, gene.strand, trans.exons[-1][0]))
                else:
                    tss_exons.add((gene.chrm, gene.strand, trans.exons[-1][0]))
                    tes_exons.add((gene.chrm, gene.strand, trans.exons[0][1] ))
                
                for intron in trans.introns:
                    all_introns.add( ( gene.chrm, gene.strand, intron ) )
        
        return ( internal_exons, tss_exons, tes_exons, 
                 all_introns, single_exon_genes )
    
    
    r_int_exons, r_tss_exons, r_tes_exons, r_introns, r_se_genes = \
        build_exon_and_intron_sets( ref_genes )

    t_int_exons, t_tss_exons, t_tes_exons, t_introns, t_se_genes = \
        build_exon_and_intron_sets( t_genes )
    
    # get overlap for each category
    int_e_overlap = float( len( r_int_exons.intersection( t_int_exons ) ) )
    tss_e_overlap = float( len( r_tss_exons.intersection( t_tss_exons ) ) )
    tes_e_overlap = float( len( r_tes_exons.intersection( t_tes_exons ) ) )
    intron_overlap = float( len( r_introns.intersection( t_introns ) ) )
    
    se_overlap = 0
    for r_se_gene in r_se_genes:
        for t_se_gene in t_se_genes:
            if t_se_gene[0] - 50 < r_se_gene[0] \
                    and t_se_gene[1] + 50 > r_se_gene[1]:
                se_overlap += 1.0
                break
    
    # get total number of exons
    num_r_exons = sum( map( len, (r_int_exons, r_tss_exons, r_tes_exons) ) ) 
    num_t_exons = sum( map( len, (t_int_exons, t_tss_exons, t_tes_exons) ) )
    
    output_stats.add_recovery_stat( \
        'Exon', num_r_exons, num_t_exons, 0 )

    output_stats.add_recovery_stat( \
        'Internal Exon', len(r_int_exons), len(t_int_exons), int_e_overlap )

    output_stats.add_recovery_stat( \
        'TSS Exon', len(r_tss_exons), len(t_tss_exons), tss_e_overlap )

    output_stats.add_recovery_stat( \
        'TES Exon', len(r_tes_exons), len(t_tes_exons), tes_e_overlap )

    output_stats.add_recovery_stat( \
        'Intron', len(r_introns), len(t_introns), intron_overlap )

    output_stats.add_recovery_stat( \
        'Single Exon Transcripts', len(r_se_genes), len(t_se_genes), se_overlap )
    
    return

def match_transcripts( ref_grp, t_grp, build_maps, build_maps_stats ):
    """find and count match classes and produce map lines if requested
    """
    def build_introns_dict( grp ):
        se_transcripts = {}
        full_transcripts = defaultdict( list )
        contains_map = defaultdict( list )
        introns_map = defaultdict( list )
        for g_name, t_name, trans in grp:
            # store single exon genes seperately. For now, se_trans are included
            # in the numbers. May want to process further in the future
            if len( trans.exons ) > 2:
                full_transcripts[ tuple( trans.exons[1:-1] ) ].append( \
                    ( g_name, t_name ) )
            else:
                bndries_tup = tuple(trans.exons)
                if bndries_tup in se_transcripts:
                    if VERBOSE:
                        print >> sys.stderr, "WARNING: found a single exon " \
                            + "transcript that is identical to another single " \
                            + "exon transcript.\n\t",
                        print t_name + ' = ' + \
                            se_transcripts[bndries_tup][1]
                else:
                    # possibly store in a bx python intervals object for overlap
                    # searching  in the future
                    se_transcripts[ bndries_tup ] = ( g_name, t_name )
            
            # for each intron, add a pointer from the first intron
            # to the remainder of the transcript. This is because, if
            # we observe that the first intron is in here, we just need to 
            # check the rest matches to verify that its a 'contains' match
            if build_maps or build_maps_stats:
                for i_index, intron in \
                        enumerate( izip( trans.exons[1::2], trans.exons[2::2] )):
                    n_skipped_introns = i_index
                    contains_map[ intron ].append(
                        ( g_name, t_name, n_skipped_introns,
                          trans.exons[ i_index*2+1: ] ) )
                    introns_map[ intron ].append( ( g_name, t_name ) )
        
        return ( full_transcripts, se_transcripts,
                 contains_map, introns_map )
    
    def cnt_trans_overlap( ref_full_trans, t_full_trans ):
        """Quickly calculate the number of exact matches
        """
        matched_cnt = 0
        for exons, transcripts in ref_full_trans.iteritems():
            if exons in t_full_trans:
                # add the number of transcripts with this internal structure
                #matched_cnt += len( transcripts )
                matched_cnt += min( len( transcripts ), \
                                    len( t_full_trans[exons] ) )
        
        # calcualte the total numebr of transcripts for the ref and t groups.
        ref_trans_tot = sum( len(i) for i in ref_full_trans.itervalues() )
        t_trans_tot = sum( len(i) for i in t_full_trans.itervalues() )
        
        return matched_cnt, ref_trans_tot, t_trans_tot
        
    def build_map_lines_and_class_counts( left_full, right_full, 
                                          right_contained, right_introns):
        map_lines = [] if build_maps else None
        class_counts = { '=':0, 'c':0, 'j':0, 'u':0 } \
            if build_maps_stats else None
        def add_map_lines( transcripts, class_cd, o_g_name, o_t_name ):
            """Add map lines with class code
            Each gene and trans name in transcripts should have the same
            internal structure so the mapping is the same
            """
            if build_maps_stats:
                class_counts[class_cd] += len(transcripts )
            
            if build_maps:
                for g_name, t_name in transcripts:
                    map_lines.append( 
                        '\t'.join( ( g_name, t_name, class_cd, 
                                     o_g_name, o_t_name)))
            
            return
        
        def get_contains_match( exons ):
            # check to see if there is a transcript that contains 
            # 'exons' ( this is a 'c' in the cuffcompare notation )
            curr_best_contains_index = None
            # loop through each transcript fragment that starts at this
            # intron, and find the best match. The best match is defined 
            # to be the one that contains all of the introns, and has
            # the least number of extraneous exons
            for i, ( r_gene_id, r_trans_id, r_n_sk_introns, r_exons ) in \
                    enumerate( right_contained[ exons[:2] ] ):
                # if the begginings match
                if list(exons) == r_exons[ :len(exons) ]:
                    # if the current best match is None, this is better
                    if curr_best_contains_index == None:
                        curr_best_contains_index = i
                        # if the best contains match is not required
                        # just return the first one found
                        if not FIND_BEST_CONTAIN_MATCH:
                            break
                    # otherwise, see how good the match is
                    # if it's better than the previous best match, 
                    # then use this one instead
                    else:
                        # calculate the number of introns in the new match
                        n_introns = r_n_sk_introns + len( r_exons )
                        # calculate the number of introns in the prev match
                        o_r_n_sk_introns = right_contained[exons[:2]][
                            curr_best_contains_index][2]
                        o_exons = right_contained[exons[:2]][
                            curr_best_contains_index][3]
                        o_n_introns = o_r_n_sk_introns + len( o_exons )
                        # if the new match is better, keep it
                        if n_introns < o_n_introns:
                            curr_best_contians_index = i
            
            return  curr_best_contains_index
        
        def get_intron_match( exons ):
            # check if there is a shared junction anywhere in the introns map
            j_match_counts = defaultdict( int )
            for intron in izip( exons[::2], exons[1::2] ):
                if intron in right_introns:
                    for j_match in right_introns[ intron ]:
                        j_match_counts[ j_match ] += 1
                        # if the best match is not requested tehn return
                        # the first one found
                        if not FIND_BEST_JN_MATCH:
                            break
            
            if len(j_match_counts) > 0:
                # find the best j_match by the one that shares the most 
                # junctions with the transcript
                max_jns_cnt, best_intron_match = 0, None
                for j_match, jns_cnt in j_match_counts.iteritems():
                    if jns_cnt > max_jns_cnt:
                        max_jns = jns_cnt
                        best_intron_match = j_match
                
                return best_intron_match
            
            # if there were no intron matches then return None
            return None
        
        
        # loop through exons and find the best match class
        for exons, transcripts in left_full.iteritems():
            # first, check to see if there is an exact match
            if exons in right_full:
                e_g_id, e_t_id = right_full[ exons ][0]
                add_map_lines( transcripts, "=", e_g_id, e_t_id )
                continue
            
            # if there is a contains match then add it
            contains_match_index = get_contains_match( exons )
            if contains_match_index != None:
                c_g_id = right_contained[exons[:2]][contains_match_index][0]
                c_t_id = right_contained[exons[:2]][contains_match_index][1]
                
                add_map_lines( transcripts, "c", c_g_id, c_t_id )
                continue
            
            # if there is an intron match add it
            intron_match = get_intron_match( exons )
            if intron_match != None:
                i_g_name, i_t_name = intron_match
                add_map_lines( transcripts, "j", i_g_name, i_t_name )
                continue
            
            # if we didn't find any better match, then add "u" class
            add_map_lines( transcripts, "u", "-", "-" )
        
        return map_lines, class_counts
    
    # produce structures to calculate match classes
    r_full_trans, r_se_trans, r_contained_map, r_introns_map \
        = build_introns_dict( ref_grp )
    t_full_trans, t_se_trans, t_contained_map, t_introns_map \
        = build_introns_dict( t_grp )
    
    # count transcripts and overlaps
    trans_overlap_cnt, r_trans_tot, t_trans_tot = \
        cnt_trans_overlap( r_full_trans, t_full_trans )    
    
    # get map lines of class counts if requested
    if build_maps or build_maps_stats:
        r_map, r_class_cnts = build_map_lines_and_class_counts(
            r_full_trans, t_full_trans, t_contained_map, t_introns_map )
        t_map, t_class_cnts = build_map_lines_and_class_counts(
            t_full_trans, r_full_trans, r_contained_map, r_introns_map )
    else:
        r_map, r_class_cnts, t_map, t_class_cnts = None, None, None, None
    
    # pack counts
    trans_counts = ( r_trans_tot, t_trans_tot, trans_overlap_cnt )
    class_counts = ( r_class_cnts, t_class_cnts )
    
    return r_map, t_map, trans_counts, class_counts

def calc_trans_cnts( all_trans_cnts, build_maps_stats, output_stats ):
    # initialize total transcript counts
    r_trans_tot, t_trans_tot, r_se_trans_tot, t_se_trans_tot = 0, 0, 0, 0
    trans_overlap, se_trans_overlap = 0, 0
    # initialize class count dicts
    r_class_cnts = { '=':0, 'c':0, 'j':0, 'u':0 } \
        if build_maps_stats else None
    t_class_cnts = { '=':0, 'c':0, 'j':0, 'u':0 } \
        if build_maps_stats else None
    
    def update_class_cnts( tot_class_cnts, new_class_cnts ):
        for class_cd, cnt in new_class_cnts.iteritems():
            tot_class_cnts[class_cd] += cnt
        return tot_class_cnts
    
    # update total counts for each group
    for grp_trans_cnts, grp_class_cnts in all_trans_cnts:
        r_trans_tot += grp_trans_cnts[0]
        t_trans_tot += grp_trans_cnts[1]
        trans_overlap += grp_trans_cnts[2]
        # if suppress_maps_stats then grp_class_cnts == None
        if build_maps_stats:
            r_class_cnts = update_class_cnts( r_class_cnts, grp_class_cnts[0] )
            t_class_cnts = update_class_cnts( t_class_cnts, grp_class_cnts[1] )

    
    output_stats.add_recovery_stat( \
        "Transcript", r_trans_tot, t_trans_tot, trans_overlap )
    
    class_cnts = ( r_class_cnts, t_class_cnts )
    
    return class_cnts

def match_all_transcripts( clustered_transcripts, build_maps, 
                           build_maps_stats, out_prefix, output_stats ):
    # open file pointers to write the maps out to
    if build_maps:
        tmap_fp = file( out_prefix + ".tmap", "w" )
        refmap_fp = file( out_prefix + ".refmap", "w" )
    else:
        tmap_fp, refmap_fp = None, None
    
    # initialize storage for all group counts
    all_trans_cnts = []
    for (chrm, strand), inner_cts in clustered_transcripts.iteritems():
        for r_grp, t_grp in inner_cts:
            r_map, t_map, grp_trans_cnts, grp_class_cnts \
                = match_transcripts(r_grp, t_grp, build_maps, build_maps_stats)
            
            if build_maps:
                if r_map != None and len(r_map) > 0:
                    refmap_fp.write( "\n".join( r_map ) + "\n" )
                if t_map != None and len(t_map) > 0:
                    tmap_fp.write( "\n".join( t_map ) + "\n" )
            
            all_trans_cnts.append( (grp_trans_cnts, grp_class_cnts) )
    
    # close the open file pointers
    if build_maps:
        refmap_fp.close()
        tmap_fp.close()
    
    return calc_trans_cnts( all_trans_cnts, build_maps_stats, output_stats )

class OutputStats( dict ):
    def __init__(self, ref_fname, gtf_fname ):
        self.ref_fname = ref_fname
        self.gtf_fname = gtf_fname
        self._recovery_stat_names = []
    
    def add_recovery_stat( self, name, ref_cnt, t_cnt, inter_cnt ):
        try:
            assert inter_cnt <= ref_cnt and inter_cnt <= t_cnt
        except:
            print inter_cnt, ref_cnt, t_cnt
            raise
        
        self[name] = ( ref_cnt, t_cnt, inter_cnt )
        self._recovery_stat_names.append( name )
        
    def __str__( self ):
        # format summary lines
        lines = []

        lines.append( "Reference:".ljust(18) + self.ref_fname )
        lines.append( "Novel Annotation:".ljust(18) + self.gtf_fname )

        lines.append( "".ljust(25) + "Recall".ljust(12) + "Precision".ljust(12)\
                      + "Intersect".ljust(12) \
                      + "Ref Cnt".ljust(12) + "Novel Cnt".ljust(12) )

        
        for stat_name in self._recovery_stat_names:
            r_cnt, t_cnt, ov_cnt = self[ stat_name ]
            lines.append( stat_name.ljust(25) + \
                          ("%.3f" % (float(ov_cnt)/(r_cnt+1e-6))).ljust(12) +
                          ("%.3f" % (float(ov_cnt)/(t_cnt+1e-6))).ljust(12) +
                          ("%i" % ov_cnt).ljust(12) +
                          ("%i" % r_cnt).ljust(12) +
                          ("%i" % t_cnt).ljust(12) )

       
        return '\n'.join( lines )

        pass

def make_class_cnts_string( class_counts, ref_fname, gtf_fname ):
    # if suppress_maps_stats then class_counts will be None and 
    # so should not be output
    if all( item == None for item in class_counts ):
        return ''
    
    r_class_cnts, t_class_cnts = class_counts
    
    class_lines = []
    
    # add class count here
    class_lines.append( "\nClass Code Counts" )    
    
    h_cts = "".join(char.ljust(12) for char in "=cju")
    class_lines.append( "".ljust(25) + h_cts )
    
    r_cts = "".join(str(r_class_cnts[char]).ljust(12) for char in "=cju")
    class_lines.append( "Reference".ljust(25) + r_cts )
    
    n_cts = "".join(str(t_class_cnts[char]).ljust(12) for char in "=cju")
    class_lines.append( "Novel Annotation".ljust(25) + n_cts )
    
    return '\n'.join( class_lines )

def compare( ref_fname, gtf_fname, build_maps, build_maps_stats, 
             out_prefix, num_threads=1 ):
    """Compare refernce to another 'gtf' annotation by element types
    """
    # load the gtf files
    ref_genes = load_gtf(ref_fname)
    t_genes = load_gtf(gtf_fname)
                 
    output_stats = OutputStats( ref_fname, gtf_fname )
    
    # get recall and prceision stats for all types of exons and introns
    build_element_stats(ref_genes, t_genes, output_stats)
    if VERBOSE: print >> sys.stderr, "Finished building element stats"
    
    clustered_transcripts = cluster_overlapping_genes( (ref_genes, t_genes) )
    if VERBOSE:
        n_clusters = sum(len(val) for val in clustered_transcripts.itervalues())
        print >> sys.stderr, \
            "Finished clustering genes into %i clusters." % n_clusters
    
    # calculate transcript overlaps and class match counts
    # also write map files if requested
    trans_class_cnts = \
        match_all_transcripts( clustered_transcripts, build_maps, 
                               build_maps_stats, out_prefix, output_stats )
        
    if out_prefix == None:
        # dump stats to stdout
        print str(output_stats) + '\n'
    else:
        # prepare formated stats output
        op = [ str(output_stats), ]
        if build_maps_stats:
            op.append( make_class_cnts_string(
                    trans_class_cnts, ref_fname, gtf_fname ) )
        
        with open( out_prefix + ".stats", "w" ) as stats_fp:
            stats_fp.write( "\n".join(op) + '\n' )
        
        if VERBOSE:
            print "\n".join( op ) + '\n'
    
    return

def parse_arguments():
    # if we just want to run unit tests
    if len( sys.argv ) > 1 and sys.argv[1] == "--test":
        return ( True, 1, "test.t.gtf", "test.ref.gtf",
                 True, True, "test.slicompare" )
    
    import argparse
    desc = 'Get exon from a variety of sources.'
    parser = argparse.ArgumentParser(description=desc)    
    parser.add_argument(
        "gtf", type=str,
        help='GTF file which contains exons associated with transcripts.'
        + ' (i.e. from build_transcripts)')
    parser.add_argument(
        "reference", type=str,
        help='Reference GTF file. (i.e. Flybase')
    
    parser.add_argument(
        '--only-build-stats', default=False, action='store_true', 
        help='Write a CSV line containing stats to stdout and do nothing else.')
    parser.add_argument(
        '--suppress-maps-stats', '-s', dest='build_maps_stats', 
        default=True, action='store_false',
        help='Whether to suppress the map stats file output.')
    parser.add_argument(
        '--build-maps', '-m', default=False, action='store_true',
        help='Whether to produce refmap and tmap files.')
    parser.add_argument(
        '--best-contains-match', '-c', default=False, action='store_true',
        help='Whether to find the optimal contains class match in the '
        + 'map files.')
    parser.add_argument(
        '--best-junctions-match', '-j', default=False, action='store_true',
        help='Whether to find the optimal junction class match in the '
        + 'map files.')
    
    parser.add_argument(
        '--out-prefix', '-o', default='gritcompare',
        help='Output file prefix will be written. default: %(default)s')
    parser.add_argument(
        '--test', default=False, action='store_true',
        help='Run unit tests.')
    parser.add_argument(
        '--threads', "-t", default=1, type=int,
        help='Set the number of threads to use.')
    parser.add_argument(
        '--verbose', '-v', default=False, action='store_true',
        help='Whether or not to print status information.')
    
    args = parser.parse_args()
    
    global VERBOSE
    VERBOSE = args.verbose
    
    global FIND_BEST_CONTAIN_MATCH 
    global FIND_BEST_JN_MATCH
    FIND_BEST_CONTAIN_MATCH = args.best_contains_match
    FIND_BEST_JN_MATCH = args.best_junctions_match
    
    # only produce stats is just short for outputing only the 
    # recall and precision for the annotations
    if args.only_build_stats:
        args.build_maps_stats = False
        args.build_maps = False
        args.out_prefix = None
    
    # if maps are not being build find just the first contains or jn match
    # in order to save computation time
    if not args.build_maps:
        FIND_BEST_CONTAIN_MATCH = False
        FIND_BEST_JN_MATCH = False
    
    return args.test, args.threads, args.gtf, args.reference, \
        args.build_maps, args.build_maps_stats, args.out_prefix

def main():
    test, n_threads, gtf, reference, build_maps, build_maps_stats, \
        out_prefix = parse_arguments()
    
    # if we want unit tests, build the data and run them
    if test:
        build_test_data()
    
    compare( reference, gtf, build_maps, build_maps_stats, 
             out_prefix, n_threads )
    
    return

if __name__ == "__main__":
    if DO_PROFILE:
        import cProfile
        cProfile.run("main()")
    else:
        main()
