# Copyright (c) 2011-2012 Nathan Boley

import sys, os

sys.path.insert( 0, os.path.join( os.path.dirname( __file__ ), \
                                      "./file_types/" ) )
sys.path.insert( 0, os.path.join( os.path.dirname( __file__ ), \
                                      "../sparsify/" ) )
from sparsify_transcripts import build_reads_objs
from gene_models import GeneBoundaries
from old_reads import BinnedReads

def usage():
    print "build_gene_cnts.py input.gtf input.bam"

def main():
    if len( sys.argv ) != 3:
        usage()
        sys.exit()
    
    genes_fname = sys.argv[1]
    gtf_fp = open( genes_fname )
    genes = GeneBoundaries( gtf_fp )
    
    bam_fname = sys.argv[2]    
    # load the read objects, possible loading an already estimated fl dist
    reads = build_reads_objs( [ bam_fname,], None, None ).values()[0]

    for gene in genes.values():
        br_cnts = BinnedReads( gene, reads, \
                               reads.read_group_mappings ).binned_reads
        print gene.name.ljust(50), sum( br_cnts.values() )    


main()
