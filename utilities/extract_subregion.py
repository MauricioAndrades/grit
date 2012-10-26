# Copyright (c) 2011-2012 Nathan Boley

import sys
import os
import subprocess
"""
example command:
python filter_bam_by_region.py `ls /media/scratch/RNAseq/all_samples/ | grep -P '^Ad|^L3_|^WPP_'`
"""

region_chr = "4"
start = 1
stop = 1351857
base_dir = "/media/scratch/dros_trans_v4/chr4/DATA/"

EXTRACT_WIG_CMD = os.path.join( os.path.dirname( __file__ ), "extract_region_from_wiggle.py" )
EXTRACT_GFF_CMD = os.path.join( os.path.dirname( __file__ ), "extract_region_from_gff.py" )
global_region_str = "%s_%i_%i" % ( region_chr, start, stop )

def build_extract_bam_cmd( sample_type, sample_id, fname, datatype=None ):
    #new_fname = os.path.join( base_dir, ".".join(\
    #        os.path.basename(fname).split(".")[:-1]) \
    #        + ".%s_%i_%i.bam" % (  region_chr, start, stop ) )
    if datatype == 'rnaseq_total_bam':
        tmp_chr_name = "chr" + region_chr
    else:
        tmp_chr_name = region_chr
    new_fname = os.path.join(base_dir, "%s_%s_%s.bam" % ( 
            sample_type, sample_id, global_region_str ))
    cmd1 = "samtools view -bh " + fname + " " \
         +  "%s:%i-%i" % (tmp_chr_name, start, stop) +  " > " + new_fname
    cmd2 = "samtools index " + new_fname
    cmd = cmd1 + " && " + cmd2
    return cmd, new_fname

def build_extract_wig_cmd( sample_type, sample_id, strand, fname, chrm_sizes_fname):
    """
    python extract_region_from_wiggle.py 
        chr4:+:1-500000 
        /media/scratch/RNAseq/cage/CAGE_AdMatedF_Ecl_1day_Heads_Trizol_Tissues.+.wig 
        /media/scratch/genomes/drosophila/dm3.chrom.sizes 
        tmp

    """
    sample_type_str = "merged" if sample_type == "*" else sample_type
    sample_id_str = "merged" if sample_id == "*" else sample_id
    
    new_fname_prefix = "%s_%s_%s" % (
        sample_type_str, sample_id_str, global_region_str )
    new_fname_prefix = os.path.join(base_dir, new_fname_prefix )
    
    region_str = "%s:%s:%i-%i" % ( region_chr, strand, start, stop )
    
    cmd_template  = "python %s {0} {1} {2} {3}" % EXTRACT_WIG_CMD
    call = cmd_template.format( 
        region_str, fname, chrm_sizes_fname, new_fname_prefix )
    
    new_fname = new_fname_prefix + ".%s.wig" % strand
    return call, new_fname

def build_extract_g_f_cmd( fname ):
    #new_fname = os.path.join( base_dir, ".".join(\
    #        os.path.basename(fname).split(".")[:-1]) \
    #        + ".%s_%i_%i.bam" % (  region_chr, start, stop ) )
    new_fname = os.path.join(base_dir, os.path.basename(fname)
                             + global_region_str + "." + fname.split(".")[-1] )

    region_str = "%s:%s:%i-%i" % ( region_chr, '.', start, stop )
    
    cmd_template  = "python %s {0} {1} > {2}" % EXTRACT_GFF_CMD
    call = cmd_template.format( region_str, fname, new_fname )
    
    return call, new_fname

def get_filetype_from_datatype( datatype ):
    if datatype.lower().endswith( "bam" ):
        return 'bam'
    elif datatype.lower().endswith( "wig" ):
        return "wig"
    elif datatype.lower().endswith( "gff" ) \
            or datatype.lower().endswith( "gtf" ):
        return "gff"
    else:
        return "UNKNOWN"
    assert False

def get_cmds_from_input_file( fp ):
    chrm_sizes_fname = None
    new_lines = []
    cmds = []
    for line_num, line in enumerate(fp):
        line = line.strip()
        
        # skip commented out lines
        if line.startswith( "#" ):
            new_lines.append( line.strip() )
            continue
        
        if line == "": continue
        
        try:
            datatype, sample_type, sample_id, strand, fname = line.split()
        except:
            print line_num, line
            raise
        # deal with the chrm sizes specially
        if datatype == 'chrm_sizes':
            new_lines.append( line.strip() )
            chrm_sizes_fname = fname
            continue
        
        filetype = get_filetype_from_datatype( datatype )
        if filetype == 'UNKNOWN':
            new_lines.append( line.strip() )
            continue
        else:
            cmd, op_fname = None, None
            if filetype == 'bam':
                cmd, op_fname = build_extract_bam_cmd(
                    sample_type, sample_id, fname, datatype)
            elif filetype == 'wig':
                cmd, op_fname = build_extract_wig_cmd(
                    sample_type, sample_id, strand, fname, chrm_sizes_fname)
            elif filetype in ('gff', 'gtf'):
                cmd, op_fname = build_extract_g_f_cmd( fname )
            else:
                print line
                assert False
        
            cmds.append( cmd )
            new_lines.append( "\t".join(
                    (datatype, sample_type, sample_id, strand, op_fname)))

    ofp = open( os.path.join( base_dir, "test_elements.txt"), "w" )
    ofp.write( "\n".join( new_lines ) )
    ofp.close()
    
    ps = []
    for cmd in cmds:
        ps.append( subprocess.Popen( cmd, shell=True ) )
    
    for p in ps:
        print p
        p.wait()
    
    return

if __name__ == "__main__":
    with open( sys.argv[1] ) as fp:
        get_cmds_from_input_file( fp )    
