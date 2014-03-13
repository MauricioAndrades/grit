import os, sys
import subprocess
from collections import defaultdict, namedtuple
from itertools import chain
import sqlite3

sys.path.insert( 0, os.path.join( os.path.dirname( __file__ ), ".." ) )
from grit.files.gtf import load_gtf
from grit.files.reads import (
    MergedReads, RNAseqReads, CAGEReads, RAMPAGEReads, PolyAReads, clean_chr_name)
from grit.lib.logging import Logger

import grit.find_elements


VERBOSE = False
NTHREADS = 1
log_statement = None

JUST_PRINT_COMMANDS = False

ControlFileEntry = namedtuple('ControlFileEntry', [
        'sample_type', 'rep_id', 
        'assay', 'paired', 'stranded', 'read_type', 
        'filename'])

def run_find_elements( promoter_reads, rnaseq_reads, polya_reads,
                       ofprefix, reference_genes, ref_elements_to_include, 
                       region_to_use ): 
                       #all_cage_reads, all_rampage_reads, all_polya_reads, 
                       #ofprefix, args ):
    print all_rnaseq_reads
    assert False
    elements_ofname = ofprefix + ".elements.bed"
        
    command = [ "python", 
                os.path.join( os.path.dirname(__file__), 
                              "..", "grit/find_elements.py" ) ]
    command.extend( chain(("--rnaseq-reads",), (
               rnaseq_reads for rnaseq_reads in all_rnaseq_reads) ))
    command.extend( chain(("--rnaseq-read-type",), (
               rnaseq_read_type for rnaseq_read_type in all_rnaseq_read_types)))
    if all_num_mapped_rnaseq_reads != None:
        all_num_mapped_rnaseq_reads = sum(all_num_mapped_rnaseq_reads)
        command.extend( ("--num-mapped-rnaseq-reads",
                         str(num_mapped_rnaseq_reads) ))
    if len(all_cage_reads) > 0:
        command.extend( chain(("--cage-reads",), (
               cage_reads for cage_reads in all_cage_reads) ) )
    if len(all_rampage_reads) > 0:
        command.extend( chain(("--rampage-reads",), (
               rampage_reads for rampage_reads in all_rampage_reads) ) )
    if len(all_polya_reads) > 0:
        command.extend( chain(("--polya-reads",), (
               polya_reads for polya_reads in all_polya_reads) ) )
    
    if args.reference != None: command.extend( 
        ("--reference", args.reference.name) )
    if args.use_reference_genes: command.append( "--use-reference-genes" )
    if args.use_reference_junctions: command.append("--use-reference-junctions")
    if args.use_reference_tss: command.append("--use-reference-tss")
    if args.use_reference_tes: command.append("--use-reference-tes")
    if args.use_reference_promoters: command.append("--use-reference-promoters")
    if args.use_reference_polyas: command.append("--use-reference-polyas")

    if args.ucsc: command.append("--ucsc")

    command.extend( ("--ofname", elements_ofname) )
    if args.batch_mode: command.append( "--batch-mode" )
    if args.region != None: command.extend( ("--region", "%s" % args.region) )
    command.extend( ("--threads", str(args.threads)) )
    if args.verbose: command.append( "--verbose" )

    subprocess.check_call(command)
    
    return elements_ofname

def run_build_transcripts(elements_fname, ofprefix,
                          rnaseq_reads, rnaseq_read_types, 
                          num_mapped_rnaseq_reads,
                          cage_reads, rampage_reads, polya_reads, args):
    transcripts_ofname = ofprefix + ".transcripts.gtf"
    expression_ofname = ofprefix + ".isoforms.fpkm_tracking"
    
    assert len(rnaseq_reads) == 1
    assert len(rnaseq_read_types) == 1
    assert len(cage_reads) <= 1
    assert len(rampage_reads) <= 1
    assert len(polya_reads) <= 1
    
    command = [ "python", 
                os.path.join( os.path.dirname(__file__), 
                              "..", "grit/build_transcripts.py" ) ]
    command.extend( ("--ofname", transcripts_ofname) )
    command.extend( ("--expression-ofname", expression_ofname) )
    
    command.extend( ("--elements", elements_fname) )    
    

    if args.only_build_candidate_transcripts:
        command.append( "--only-build-candidate-transcripts" )
    else:
        command.extend( ("--rnaseq-reads", rnaseq_reads[0]) )
        command.extend( ("--rnaseq-read-type", rnaseq_read_types[0]) )
        if num_mapped_rnaseq_reads != None: 
            command.extend( ("--num-mapped-rnaseq-reads",
                             str(num_mapped_rnaseq_reads) ))    
        if len(cage_reads) > 0: 
            command.extend( ("--cage-reads", cage_reads[0]) )
        if len(rampage_reads) > 0:
            command.extend( ("--rampage-reads", rampage_reads[0]) )

        if len(polya_reads) > 0:
            command.extend( ("--polya-reads", polya_reads[0]) )
        
        command.append( "--estimate-confidence-bounds" )

    
    if args.fasta != None: command.extend( ("--fasta", args.fasta.name) )
    
    if args.batch_mode: command.append( "--batch-mode" )
    command.extend( ("--threads", str(args.threads)) )
    if args.verbose: command.append( "--verbose" )

    if args.ucsc: command.append("--ucsc")
    
    subprocess.check_call(command)
    
    return

def run_bam2wig(fname, op_prefix, assay, region,
                nthreads, reverse_read_strand, verbose):
    print >> sys.stderr, "Building bedgraph for %s" % fname
    assert assay in ["rnaseq", "cage", "rampage", "polya"], \
        "Unrecognized assay '%s'" % assay
    command = ["python", os.path.join(os.path.dirname(__file__), "bam2wig.py" )]
    command.extend( ("--mapped-reads-fname", fname ))
    command.extend( ("--out-fname-prefix", op_prefix ))
    command.extend( ("--assay",  assay))
    command.extend( ("--threads",  str(nthreads)))
    if reverse_read_strand:
        command.append( "--reverse-read-strand" )
    if verbose: command.append( "--verbose" )
    if region != None: command.extend( ("--region", "%s" % region) )
    subprocess.check_call(command)

def run_all_bam2wigs(conn, args):
    for rnaseq_reads, rnaseq_reads_type in get_elements( 
            conn, ('filename', 'read_type'), 'rnaseq'):
        run_bam2wig(rnaseq_reads, os.path.basename(rnaseq_reads),
                    'rnaseq', args.region,
                    args.threads, bool(rnaseq_reads_type=='backward'),
                    args.verbose)
    for data_type in ('cage', 'rampage', 'polya'):
        for reads, in get_elements( conn, ('filename', ), data_type):
            # unfortunately, sqllite returns none for an empty query
            if reads == None: continue
            run_bam2wig(reads, os.path.basename(reads),
                        data_type, args.region,
                        args.threads, False, args.verbose)
    return

class Samples(object):
    """Store and retrieve sample information.

    """
    def parse_control_file(self, control_fp):
        lines = []
        for line in control_fp:
            if line.strip().startswith("#"): continue
            if line.strip() == '': continue
            lines.append( ControlFileEntry(*(line.split())) )
        return lines
    
    def parse_single_sample_args(self, args):
        """Parse read data passed in as arguments.

        """
        lines = []
        lines.append( ControlFileEntry( 
                None, None, "rnaseq", True, True, 
                args.rnaseq_read_type, args.rnaseq_reads.name ) )
        if args.cage_reads != None:
            lines.append( ControlFileEntry( 
                    None, None, "cage", False, True, 
                    args.cage_read_type, args.cage_reads.name ) )
        if args.rampage_reads != None:
            lines.append( ControlFileEntry( 
                    None, None, "rampage", True, True, 
                    args.rampage_read_type, args.rampage_reads.name ) )
        if args.polya_reads != None:
            lines.append( ControlFileEntry( 
                    None, None, "polya", False, True,
                    args.polya_read_type, args.polya_reads.name ) )
        return lines
    
    def verify_args_are_sufficient(self, rnaseq_reads, promoter_reads, polya_reads):
        if ( len(promoter_reads) == 0
             and not self.args.use_reference_tss
             and not self.args.use_reference_promoters):
            raise ValueError, "Either (cage reads or rampage reads) must be provided for each sample or (--use-reference-tss or --use-reference-promoters) must be set"
        
        if ( len(polya_reads) == 0
             and not self.args.use_reference_tes
             and not self.args.use_reference_polyas ):
            raise ValueError, "Either polya-reads must be provided or (--use-reference-tes or --use-reference-polyas) must be set"
        
        return
    
    def initialize_sample_db(self):
        self.conn = sqlite3.connect(':memory:')
        with self.conn:
            self.conn.execute("""
            CREATE TABLE data (
               sample_type text,
               rep_id text,
               assay text,
               paired text,
               stranded text,
               read_type text,
               filename text
            );""")
    
    def __init__(self, args):
        # the args object returned by parse arguments
        self.args = args
        # cache mapped reads objects that have already been initialized
        self.mapped_reads_cache = {}
        # store parsed reference genes, if necessary
        self.ref_genes = None
        # initialize a sqlite db to store samples
        self.initialize_sample_db()
        # parse the control data
        if args.control == None:
            self.control_entries = self.parse_single_sample_args( args )
        else:
            self.control_entries = self.parse_control_file(args.control)
            
        # if any of the read_type arguments are 'auto', then load the
        # reference genome
        if any(x.read_type == 'auto' for x in self.control_entries):
            if args.reference == None:
                raise ValueError, "One of the read_type entries is set to 'auto' but a reference was not provided"
            if VERBOSE: log_statement("Loading annotation file.")
            self.ref_genes = load_gtf( args.reference )
        
        # insert the various data sources into the database
        with self.conn:
            self.conn.executemany( """
            INSERT INTO data VALUES(?, ?, ?, ?, ?, ?, ?)
            """, self.control_entries )
        
        return
    
    def __str__(self):
        header = "#%s\n#\n" % ("\t".join([
                    'sample_type', 'rep_id', 'assay', 
                    'paired', 'stranded', 'read_type', 'filename']))
        return header + "\n".join("\t".join(x) for x in self.control_entries)

    def get_elements( self, assay, sample_type=None, rep_id=None ):
        """Get values of the specified column name, optionally filtered by
           sample_type and rep_id
        """
        # get the data
        query = "SELECT * FROM data WHERE assay='{}'".format(assay)
        
        if rep_id != None: 
            assert sample_type != None, \
                "rep_id can't be filtered without a sample type filter"
        if sample_type != None:
            query += " AND (sample_type = '{}' OR sample_type = '*') ".format(
                sample_type)
        if rep_id != None:
            query += " AND (rep_id = '{}' OR rep_id = '*') ".format(rep_id)
        with self.conn:
            return [ ControlFileEntry(*x) for x in 
                     self.conn.execute( query  ).fetchall() ]

    def _load_rnaseq_reads(self, sample_type, rep_id):
        all_reads = []
        for data in self.get_elements( 'rnaseq', sample_type, rep_id ):
            if data.filename in self.mapped_reads_cache:
                reads = self.mapped_reads_cache[data.filename]
                reads.reload()
            else:
                assert data.paired == 'true', "RNASeq reads must be paired"
                assert data.stranded == 'true', "RNASeq reads must be stranded"
                rev_reads = {'forward':False, 'backward':True, 'auto': None}[
                    data.read_type]
                reads = RNAseqReads(data.filename)
                reads.init(reverse_read_strand=rev_reads, ref_genes=self.ref_genes)
                self.mapped_reads_cache[data.filename] = reads
            all_reads.append(reads)
        
        return all_reads

    def _load_promoter_reads(self, sample_type, rep_id):
        cage_elements = self.get_elements( 'cage', sample_type, rep_id )
        rampage_elements = self.get_elements( 'rampage', sample_type, rep_id )

        assert len(cage_elements) == 0 or len(rampage_elements) == 0, \
            "Can not use both RAMPAGE and CAGE reads in a single sample"
        if len(cage_elements) > 0: 
            elements = cage_elements
            reads_class = CAGEReads
        elif len(rampage_elements) > 0:
            elements = rampage_elements
            reads_class = RAMPAGEReads
        else: 
            return []

        promoter_reads = []
        for data in elements:
            if data.filename in self.mapped_reads_cache:
                reads = self.mapped_reads_cache[data.filename]
                reads.reload()
            else:
                rev_reads = {'forward':False, 'backward':True, 'auto': None}[
                    data.read_type]
                reads = reads_class(data.filename)
                reads.init(reverse_read_strand=rev_reads, ref_genes=self.ref_genes)
                self.mapped_reads_cache[data.filename] = reads
            promoter_reads.append(reads)
        
        return promoter_reads

    def _load_polya_reads(self, sample_type, rep_id):
        all_reads = []
        for data in self.get_elements( 'polya', sample_type, rep_id ):
            if data.filename in self.mapped_reads_cache:
                reads = self.mapped_reads_cache[data.filename]
                reads.reload()
            else:
                assert data.stranded == 'true', "polya-site-seq reads must be stranded"
                rev_reads = {'forward':False, 'backward':True, 'auto': None}[
                    data.read_type]
                reads = PolyAReads(data.filename)
                reads.init(reverse_read_strand=rev_reads, ref_genes=self.ref_genes)
                self.mapped_reads_cache[data.filename] = reads
            all_reads.append(reads)
        
        return all_reads
    
    def get_reads(self, sample_type=None, rep_id=None):
        rnaseq_reads = self._load_rnaseq_reads(sample_type, rep_id)
        promoter_reads = self._load_promoter_reads(sample_type, rep_id)
        polya_reads = self._load_polya_reads(sample_type, rep_id)
        self.verify_args_are_sufficient( 
            rnaseq_reads, promoter_reads, polya_reads )
        return ( MergedReads(promoter_reads), 
                 MergedReads(rnaseq_reads), 
                 MergedReads(polya_reads) )
    
    def get_sample_types(self):
        query = "SELECT DISTINCT sample_type FROM data"
        with self.conn:
            return [x[0] for x in self.conn.execute(query).fetchall()]
    

def load_ref_elements_to_include(args):
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
    return RefElementsToInclude( args.use_reference_genes, 
                                 args.use_reference_junctions,
                                 args.use_reference_tss, 
                                 args.use_reference_tes,
                                 args.use_reference_promoters,
                                 args.use_reference_polyas )

def parse_arguments():
    import argparse

    parser = argparse.ArgumentParser(
        description='Build transcripts and quantify expression levels from RNAseq, CAGE, and poly(A) assays.')

    parser.add_argument( '--control', type=file, 
        help='GRIT control file. Allows better control over the types of input files.')

    parser.add_argument( '--rnaseq-reads', type=argparse.FileType('rb'), 
        help='BAM file containing mapped RNAseq reads.')
    parser.add_argument( '--rnaseq-read-type', 
                         choices=["forward", "backward", "auto"],
                         default='auto',
        help="If 'forward' then the first RNAseq read in a pair that maps to the genome without being reverse complemented is assumed to be on the correct strand. default: auto")
    parser.add_argument( '--num-mapped-rnaseq-reads', type=int,
        help="The total number of mapped rnaseq reads ( needed to calculate the FPKM ). This only needs to be set if it isn't found by a call to samtools idxstats." )
    
    parser.add_argument( '--cage-reads', type=argparse.FileType('rb'),
        help='BAM file containing mapped cage reads.')
    parser.add_argument( '--cage-read-type', 
                         choices=["forward", "backward", "auto"],
                         default='auto',
        help="If 'forward' then the reads that maps to the genome without being reverse complemented are assumed to be on the '+'. default: auto")

    parser.add_argument( '--rampage-reads', type=argparse.FileType('rb'),
        help='BAM file containing mapped rampage reads.')
    parser.add_argument( '--rampage-read-type', 
                         choices=["forward", "backward", "auto"],
                         default='auto',
        help="If 'forward' then the first read in a pair that maps to the genome without being reverse complemented are assumed to be on the '+' strand. default: auto")
    
    parser.add_argument( '--polya-reads', type=argparse.FileType('rb'), 
        help='BAM file containing mapped polya reads.')
    parser.add_argument( '--polya-read-type', choices=["forward", "backward", "auto"],
                         default='auto',
        help="If 'forward' then the reads that maps to the genome without being reverse complemented are assumed to be on the '+'. default: auto")

    parser.add_argument( '--build-bedgraphs', default=False,action='store_true',
                         help='Build read coverage bedgraphs.')
    
    parser.add_argument( '--reference', help='Reference GTF', type=file)
    parser.add_argument( '--fasta', type=file,
        help='Fasta file containing the genome sequence - if provided the ORF finder is automatically run.')
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

    parser.add_argument( '--only-build-candidate-transcripts', 
                         help='Do not estiamte transcript frequencies - just build trnascripts.',
                         default=False, action='store_true')

    parser.add_argument( '--ofprefix', '-o', default="discovered",
        help='Output files prefix. (default: discovered)')
    
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
    parser.add_argument( '--region', 
        help='Only use the specified region ( currently only accepts a contig name ).')
    
    parser.add_argument( '--threads', '-t', default=1, type=int,
        help='The number of threads to use.')
        
    args = parser.parse_args()
        
    if None == args.control and None == args.rnaseq_reads:
        raise ValueError, "--control or --rnaseq-reads must be set"

    global VERBOSE
    VERBOSE = args.verbose
    grit.find_elements.VERBOSE = VERBOSE
    grit.files.junctions.VERBOSE = VERBOSE
    
    global DEBUG_VERBOSE
    DEBUG_VERBOSE = args.debug_verbose
    grit.find_elements.DEBUG_VERBOSE = args.debug_verbose

    global WRITE_DEBUG_DATA
    WRITE_DEBUG_DATA = args.write_debug_data
    grit.find_elements.WRITE_DEBUG_DATA = args.write_debug_data
    
    global NTHREADS
    NTHREADS = args.threads
    grit.find_elements.NTHREADS = NTHREADS
    
    global TOTAL_MAPPED_READS
    TOTAL_MAPPED_READS = 1e6
    grit.find_elements.TOTAL_MAPPED_READS = 1e6

    global FIX_CHRM_NAMES_FOR_UCSC
    FIX_CHRM_NAMES_FOR_UCSC = args.ucsc
    grit.find_elements.FIX_CHRM_NAMES_FOR_UCSC = args.ucsc
    
    global log_statement
    log_ofstream = open( args.ofprefix + ".log", "w" )
    log_statement = Logger(
        nthreads=NTHREADS+1, 
        use_ncurses=(not args.batch_mode), 
        log_ofstream=log_ofstream)
    grit.find_elements.log_statement = log_statement
    
    args.region = clean_chr_name(args.region)
    
    return args

def main():
    args = parse_arguments()

    # parse the reference elements to include
    ref_elements_to_include = load_ref_elements_to_include(args)
    
    # load the samples into database, and the reference genes if necessary
    sample_data = Samples(args)

    # if the reference genes weren't loaded while parsing the data, and we
    # need the reference elements, then load the reference genes now
    if any(ref_elements_to_include) and sample_data.ref_genes == None:
        if VERBOSE: log_statement("Loading annotation file.")
        sample_data.ref_genes = load_gtf(args.reference)
    
    for sample_type in sample_data.get_sample_types():
        ofp = open("%s.%s.elements.bed" % (args.ofprefix, sample_type), "w")
        
        promoter_reads, rnaseq_reads, polya_reads = sample_data.get_reads(
            sample_type)
        
        elements_fname = grit.find_elements.find_elements(
            promoter_reads, rnaseq_reads, polya_reads,
            ofp, sample_data.ref_genes, ref_elements_to_include, 
            region_to_use=args.region)
        
        """
        # if we used a control file, and thus have sample types, then find 
        # the unqiue repids for this sample
        if sample_type != None:
            query = "SELECT DISTINCT rep_id FROM data \
                     WHERE sample_type = '{}' AND rep_id != '*'"
            rep_ids = [ x[0] for x in 
                        conn.execute(query.format(sample_type)).fetchall()]
        # otherwise, use everything by setting hte rep id to None
        else: rep_ids = [None,]
        
        for rep_id in rep_ids:
            ( rnaseq_reads, rnaseq_read_types, cage_reads, rampage_reads,
              polya_reads ) = get_run_data(conn, args, sample_type, rep_id)

            ofprefix = args.ofprefix
            if sample_type != None: ofprefix += ("." + sample_type)
            if rep_id != None: ofprefix += ("." + rep_id)
            
            run_build_transcripts( 
                elements_fname, ofprefix, 
                rnaseq_reads, rnaseq_read_types, 
                args.num_mapped_rnaseq_reads,
                cage_reads, rampage_reads, polya_reads, args)
                """

if __name__ == '__main__':
    main()
