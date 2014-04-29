import sys, os
import shutil

import time
import traceback

import numpy

from pysam import Fastafile

from itertools import chain
from collections import namedtuple

import multiprocessing
from multiprocessing.sharedctypes import RawArray, RawValue
from lib.multiprocessing_utils import Pool

import networkx as nx

from lib.multiprocessing_utils import ThreadSafeFile
from transcript import Transcript, Gene
from files.reads import fix_chrm_name_for_ucsc
from proteomics.ORF import find_cds_for_gene
from elements import \
    load_elements, cluster_elements, find_jn_connected_exons

import config

import cPickle as pickle


GeneElements = namedtuple('GeneElements', 
                          ['id', 'chrm', 'strand',
                           'tss_exons', 'internal_exons', 'tes_exons',
                           'se_transcripts', 'promoter', 'polyas', 
                           'introns'])


def build_transcripts_from_elements( 
        tss_exons, internal_exons, tes_exons, se_transcripts, jns, strand ):
                       
    # build a directed graph, with edges leading from exon to exon via junctions
    all_exons = sorted(chain(tss_exons, internal_exons, tes_exons))
    graph = nx.DiGraph()
    graph.add_nodes_from( tss_exons )
    graph.add_nodes_from( internal_exons )
    graph.add_nodes_from( tes_exons )
    
    edges = find_jn_connected_exons(all_exons, jns, strand )
    graph.add_edges_from( (start, stop) for jn, start, stop in edges )
    transcripts = []
    for tss in tss_exons:
        for tes in tes_exons:
            cutoff = config.MAX_NUM_CANDIDATE_TRANSCRIPTS-len(transcripts)+1
            for transcript in nx.all_simple_paths(graph, tss, tes, cutoff):
                transcripts.append( sorted(transcript) )
                if len(transcripts) > config.MAX_NUM_CANDIDATE_TRANSCRIPTS:
                    raise ValueError, "Too many candidate transcripts"
    
    return transcripts + [ [x,] for x in se_transcripts ]

class MaxIterError( ValueError ):
    pass

def write_gene_to_gtf( ofp, gene ):
    lines = []
    for index, transcript in enumerate(gene.transcripts):
        meta_data = {}
        
        if config.FIX_CHRM_NAMES_FOR_UCSC:
            transcript.chrm = fix_chrm_name_for_ucsc(transcript.chrm)
        lines.append( transcript.build_gtf_lines(
                transcript.gene_id, meta_data, source="grit") + "\n" )
    
    ofp.write( "".join(lines) )
    
    return

def find_matching_promoter_for_transcript(transcript, promoters):
    # find the promoter that starts at the same basepair
    # If it extends beyond the first exon, we truncate the
    # promoter at the end of the first exon
    tss_exon = transcript.exons[0] if transcript.strand == '+' \
        else transcript.exons[-1] 
    matching_promoter = None
    for promoter in promoters:
        if transcript.strand == '-' and promoter[1] == tss_exon[1]:
            matching_promoter = (max(promoter[0], tss_exon[0]), promoter[1])
        elif transcript.strand == '+' and promoter[0] == tss_exon[0]:
            matching_promoter = (promoter[0], min(promoter[1], tss_exon[1]))
    
    return matching_promoter

def find_matching_polya_region_for_transcript(transcript, polyas):
    # find the polya that ends at the same basepair
    # If it extends beyond the tes exon, we truncate the
    # polya region
    tes_exon = transcript.exons[-1] if transcript.strand == '+' \
        else transcript.exons[0] 
    matching_polya = None
    for polya in polyas:
        if transcript.strand == '+' and polya[1] == tes_exon[1]:
            matching_polya = (max(polya[0], tes_exon[0]), polya[1])
        elif transcript.strand == '-' and polya[0] == tes_exon[0]:
            matching_polya = (polya[0], min(polya[1], tes_exon[1]))
    
    return matching_polya

def rename_transcripts(gene, ref_genes):
    # find the ref genes that overlap gene
    ref_genes = list(ref_genes.iter_overlapping_genes(
            gene.chrm, gene.strand, gene.start, gene.stop))
    if len(ref_genes) == 0:
        return gene

    cntr = 1 
    
    for t in gene.transcripts:
        best_match = None
        best_match_score = (0, -1e9, 0)
        introns = set(t.introns)
        for ref_gene in ref_genes:
            for ref_t in ref_gene.transcripts:
                score = (
                    len(introns) - len(set(introns)-set(ref_t.introns)),
                    -(abs(ref_t.start-t.start)+abs(ref_t.stop-t.stop)),
                    0 )
                if score > best_match_score:
                    best_match_score = score
                    best_match = ref_t

        if best_match == None: continue
        t.ref_gene = best_match.gene_id
        t.ref_trans = best_match.id

        t.gene_id = t.ref_gene
        if len(introns)  == len(best_match.introns) == best_match_score[0] and \
                best_match_score[1] > -400:
            t.id = t.ref_trans
        elif len(introns) == len(best_match.introns) == best_match_score[0]:
            t.id = t.ref_trans + '-NOBNDS'
        #elif best_match_score[0] > 0:
        #    t.id = t.ref_trans + "-PARTIAL"
        else:
            t.id = t.gene_id + "-MDV4-%i" % cntr
            cntr += 1
        #print t.id, ref_t.id, best_match_score, len(introns)

    gene_ids = set(t.gene_id for t in gene.transcripts)
    gene.id = "/".join(gene_ids)
    return gene

def build_gene(elements, fasta=None, ref_genes=None):
    gene_min = min( min(e) for e in chain(
            elements.tss_exons, elements.tes_exons, elements.se_transcripts))
    gene_max = max( max(e) for e in chain(
            elements.tss_exons, elements.tes_exons, elements.se_transcripts))
        
    transcripts = []
    for i, exons in enumerate( build_transcripts_from_elements( 
            elements.tss_exons, elements.internal_exons, elements.tes_exons,
            elements.se_transcripts, elements.introns, elements.strand ) ):
        transcript = Transcript(
            "%s_%i" % ( elements.id, i ), elements.chrm, elements.strand, 
            exons, cds_region=None, gene_id=elements.id)
        transcript.promoter = find_matching_promoter_for_transcript(
            transcript, elements.promoter)
        transcript.polya_region = find_matching_polya_region_for_transcript(
            transcript, elements.polyas)
        transcripts.append( transcript )

    gene = Gene(elements.id, elements.chrm, elements.strand, 
                gene_min, gene_max, transcripts)

    if fasta != None:
        gene.transcripts = find_cds_for_gene( 
            gene, fasta, only_longest_orf=True )
    
    if ref_genes != None:
        gene = rename_transcripts(gene, ref_genes)

    return gene

def worker( elements, elements_lock, 
            output, output_lock, gtf_ofp,
            fasta_fp, ref_genes ):
    # if appropriate, open the fasta file
    if fasta_fp != None: fasta = Fastafile(fasta_fp.name)
    else: fasta = None
    
    while True:
        try:
            with elements_lock:
                gene_elements = elements.pop()
        except IndexError:
            config.log_statement("")
            return

        config.log_statement(
            "Building transcript and ORFs for Gene %s" % gene_elements.id)
        
        # build the gene with transcripts, and optionally call orfs
        try:
            gene = build_gene(gene_elements, fasta, ref_genes)
            config.log_statement(
                "FINISHED Building transcript and ORFs for Gene %s" % gene.id)

            # dump a pickel of the gene to a temp file, and set that in the 
            # output manager
            ofname = gene.write_to_temp_file(config.tmp_dir)
            with output_lock: 
                output.append((gene.id, len(gene.transcripts), ofname))
            
            write_gene_to_gtf(gtf_ofp, gene)
        except Exception, inst:
            config.log_statement(
                "ERROR building transcript in %s: %s"%(gene_elements.id, inst))
            if config.DEBUG_VERBOSE:
                config.log_statement( traceback.format_exc(), log=True )
    
    return

def add_elements_for_contig_and_strand((contig, strand), grpd_exons,
                                       elements, elements_lock):
    gene_id_num = 1
    config.log_statement( 
        "Clustering elements into genes for %s:%s" % ( contig, strand ) )
    for ( tss_es, tes_es, internal_es, 
          se_ts, promoters, polyas, jns ) in cluster_elements( 
            set(map(tuple, grpd_exons['tss_exon'].tolist())), 
            set(map(tuple, grpd_exons['internal_exon'].tolist())), 
            set(map(tuple, grpd_exons['tes_exon'].tolist())), 
            set(map(tuple, grpd_exons['single_exon_gene'].tolist())),
            set(map(tuple, grpd_exons['promoter'].tolist())), 
            set(map(tuple, grpd_exons['polya'].tolist())), 
            set(map(tuple, grpd_exons['intron'].tolist())), 
            strand):
        # skip genes without all of the element types
        if len(se_ts) == 0 and (
                len(tes_es) == 0 
                or len( tss_es ) == 0 ):
            continue

        gene_id_num += 1
        gene_id = "%s_%s_%i" % ( 
            contig, 'm' if strand == '-' else 'p', gene_id_num )

        gene_data = GeneElements( gene_id, contig, strand,
                                  tss_es, internal_es, tes_es,
                                  se_ts, promoters, polyas, 
                                  grpd_exons['intron'] )
        
        with elements_lock:
            elements.append(gene_data)
    
    config.log_statement("")
    return    

def build_transcripts(exons_bed_fp, ofname, fasta_fp=None, ref_genes=None):
    """Build transcripts
    """    
    # make sure that we're starting from the start of the 
    # elements files
    config.log_statement( "Loading %s" % exons_bed_fp.name )
    exons_bed_fp.seek(0)
    raw_elements = load_elements( exons_bed_fp )
    config.log_statement( "Finished Loading %s" % exons_bed_fp.name )
    
    gtf_ofp = ThreadSafeFile(ofname + ".unfinished", "w")
    gtf_ofp.write("track name=%s useScore=1\n" 
                  % ".".join(ofname.split(".")[:-1]))
    
    manager = multiprocessing.Manager()
    elements = manager.list()
    elements_lock = manager.Lock()    
    
    all_args = []
    for (contig, strand), grpd_exons in raw_elements.iteritems():
        all_args.append([
                (contig, strand), grpd_exons, elements, elements_lock])
    
    if config.NTHREADS in (None, 1):
        for args in all_args:
            add_elements_for_contig_and_strand(*args)
    else:
        p = Pool(config.NTHREADS)
        p.apply(add_elements_for_contig_and_strand, all_args)

    output = manager.list()
    output_lock = manager.Lock()    

    args = [elements, elements_lock, 
            output, output_lock, gtf_ofp, 
            fasta_fp, ref_genes]
    if config.NTHREADS in (None, 1):
        worker(*args)
    else:
        ps = []
        for i in xrange(config.NTHREADS):
            p = multiprocessing.Process(target=worker, args=args)
            p.daemon = True
            p.start()
            ps.append(p)
        for p in ps:
            p.join()
    
    genes = sorted(output)
    manager.shutdown()
    config.log_statement("Finished building transcripts")

    gtf_ofp.close()
    shutil.move(ofname + ".unfinished", ofname)
    
    return genes

def main():
    assert False
    with open(sys.argv[1]) as fp:
        build_transcripts(fp, "tmp", sys.argv[2])

if __name__ == '__main__':
    main()
