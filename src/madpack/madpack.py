#!/usr/bin/env python
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# Main Madpack installation executable.
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
import sys
import getpass
import re
import os
import glob
import traceback
import subprocess
import datetime
import tempfile
import shutil

import upgrade_util as uu
from utilities import _write_to_file
from utilities import error_
from utilities import get_dbver
from utilities import get_db_madlib_version
from utilities import get_rev_num
from utilities import info_
from utilities import is_rev_gte
from utilities import remove_comments_from_sql
from utilities import run_query
# Required Python version
py_min_ver = [2, 6]

# Check python version
if sys.version_info[:2] < py_min_ver:
    print("ERROR: python version too old ({0}). You need {1} or greater.".
          format('.'.join(map(str, sys.version_info[:3])),
                 '.'.join(map(str, py_min_ver))))
    exit(1)

# Find MADlib root directory. This file is installed to
# $MADLIB_ROOT/madpack/madpack.py, so to get $MADLIB_ROOT we need to go
# two levels up in the directory hierarchy. We use (a) os.path.realpath and
# (b) __file__ (instead of sys.argv[0]) because madpack.py could be called
# (a) through a symbolic link and (b) not as the main module.
maddir = os.path.abspath(os.path.dirname(os.path.realpath(__file__)) + "/..")   # MADlib root dir
sys.path.append(maddir + "/madpack")

# Import MADlib python modules
import argparse
import configyml

# Some read-only variables
this = os.path.basename(sys.argv[0])    # name of this script

# Default directories
maddir_conf = maddir + "/config"           # Config dir
maddir_lib = maddir + "/lib/libmadlib.so"  # C/C++ libraries

# Read the config files
ports = configyml.get_ports(maddir_conf)  # object made of Ports.yml
new_madlib_ver = configyml.get_version(maddir_conf)  # MADlib OS-level version
portid_list = []
for port in ports:
    portid_list.append(port)

SUPPORTED_PORTS = ('postgres', 'greenplum')

# Global variables
portid = None       # Target port ID (eg: pg90, gp40)
dbver = None        # DB version
con_args = {}       # DB connection arguments
verbose = None      # Verbose flag
keeplogs = None
tmpdir = None


def _make_dir(dir):
    """
    # Create a temp dir
    # @param dir temp directory path
    """
    if not os.path.isdir(dir):
        try:
            os.makedirs(dir)
        except:
            print "ERROR: can not create directory: %s. Check permissions." % dir
            exit(1)
# ------------------------------------------------------------------------------


def _internal_run_query(sql, show_error):
    """
    Runs a SQL query on the target platform DB
    using the default command-line utility.
    Very limited:
      - no text output with "new line" characters allowed
         @param sql query text to execute
         @param show_error displays the SQL error msg
    """
    return run_query(sql, con_args, show_error)
# ------------------------------------------------------------------------------

def _get_relative_maddir(maddir, port):
    """ Return a relative path version of maddir

    GPDB installations have a symlink outside of GPHOME that
    links to the current GPHOME. After a DB upgrade, this symlink is updated to
    the new GPHOME.

    'maddir_lib', which uses the absolute path of GPHOME, is hardcoded into each
    madlib function definition. Replacing the GPHOME path with the equivalent
    relative path makes it simpler to perform DB upgrades without breaking MADlib.
    """
    if port == 'postgres':
        # do nothing for postgres
        return maddir

    # e.g. maddir_lib = $GPHOME/madlib/Versions/1.9/lib/libmadlib.so
    # 'madlib' is supposed to be in this path, which is the default folder
    # used by GPPKG to install madlib
    try:
        abs_gphome, tail = maddir.split('madlib/')
    except ValueError:
        return maddir

    # Check outside $GPHOME if there is a symlink to this absolute path
    # os.pardir is equivalent to ..
    # os.path.normpath removes the extraneous .. from that path
    rel_gphome = os.path.normpath(os.path.join(abs_gphome, os.pardir, 'greenplum-db'))
    if (os.path.islink(rel_gphome) and
            os.path.realpath(rel_gphome) == os.path.realpath(abs_gphome)):
        # if the relative link exists and is pointing to current location
        return os.path.join(rel_gphome, 'madlib', tail)
    else:
        return maddir
# ------------------------------------------------------------------------------

def _cleanup_comments_in_sqlfile(output_filename, upgrade):
    """
    @brief Remove comments in the sql script, and re-write the file with the
    cleaned up script.
    """
    if not upgrade:
        with open(output_filename, 'r+') as output_filehandle:
            full_sql = output_filehandle.read()
            full_sql = remove_comments_from_sql(full_sql)
        # Re-write the cleaned-up sql to a new file. Python does not let us
        # erase all the content of a file and rewrite the same file again.
        cleaned_output_filename = output_filename+'.tmp'
        with open(cleaned_output_filename, 'w') as output_filehandle:
            _write_to_file(output_filehandle, full_sql)
        # Move the cleaned output file to the old one.
        os.rename(cleaned_output_filename, output_filename)

def _run_m4_and_append(schema, maddir_mod_py, module, sqlfile,
                       output_filehandle, pre_sql=None):
    """
    Function to process a sql file with M4.
    """
    # Check if the SQL file exists
    if not os.path.isfile(sqlfile):
        error_(this, "Missing module SQL file (%s)" % sqlfile, False)
        raise ValueError

    # Prepare the file using M4
    try:
        # Add the before SQL
        if pre_sql:
            output_filehandle.writelines([pre_sql, '\n\n'])
        # Find the madpack dir (platform specific or generic)
        if os.path.isdir(maddir + "/ports/" + portid + "/" + dbver + "/madpack"):
            maddir_madpack = maddir + "/ports/" + portid + "/" + dbver + "/madpack"
        else:
            maddir_madpack = maddir + "/madpack"
        maddir_ext_py = maddir + "/lib/python"

        m4args = ['m4',
                  '-P',
                  '-DMADLIB_SCHEMA=' + schema,
                  '-DPLPYTHON_LIBDIR=' + maddir_mod_py,
                  '-DEXT_PYTHON_LIBDIR=' + maddir_ext_py,
                  '-DMODULE_PATHNAME=' + maddir_lib,
                  '-DMODULE_NAME=' + module,
                  '-I' + maddir_madpack,
                  sqlfile]

        info_(this, "> ... parsing: " + " ".join(m4args), verbose)
        output_filehandle.flush()
        subprocess.call(m4args, stdout=output_filehandle)
    except:
        error_(this, "Failed executing m4 on %s" % sqlfile, False)
        raise Exception

def _run_install_check_sql(schema, maddir_mod_py, module, sqlfile,
                           tmpfile, logfile, pre_sql):
    """
        Run SQL file
            @param schema name of the target schema
            @param maddir_mod_py name of the module dir with Python code
            @param module  name of the module
            @param sqlfile name of the file to parse
            @param tmpfile name of the temp file to run
            @param logfile name of the log file (stdout)
            @param pre_sql optional SQL to run before executing the file
    """
    try:
        f = open(tmpfile, 'w')
        _run_m4_and_append(schema, maddir_mod_py, module, sqlfile, f, pre_sql)
        f.close()
    except:
        error_(this, "Failed to temp m4 processed file %s." % tmpfile, False)
        raise Exception
    # Only update function definition
    sub_module = ''
    # Run the SQL using DB command-line utility
    if portid in SUPPORTED_PORTS:
        sqlcmd = 'psql'
        # Test the DB cmd line utility
        std, err = subprocess.Popen(['which', sqlcmd], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE).communicate()
        if not std:
            error_(this, "Command not found: %s" % sqlcmd, True)

        runcmd = [sqlcmd, '-a',
                  '-v', 'ON_ERROR_STOP=1',
                  '-h', con_args['host'].split(':')[0],
                  '-p', con_args['host'].split(':')[1],
                  '-d', con_args['database'],
                  '-U', con_args['user'],
                  '--no-password',
                  '-f', tmpfile]
        runenv = os.environ
        if 'password' in con_args:
            runenv["PGPASSWORD"] = con_args['password']
        runenv["PGOPTIONS"] = '-c client_min_messages=notice'

    # Open log file
    try:
        log = open(logfile, 'w')
    except:
        error_(this, "Cannot create log file: %s" % logfile, False)
        raise Exception

    # Run the SQL
    try:
        info_(this, "> ... executing " + tmpfile, verbose)
        retval = subprocess.call(runcmd, env=runenv, stdout=log, stderr=log)
    except:
        error_(this, "Failed executing %s" % tmpfile, False)
        raise Exception
    finally:
        log.close()

    return retval
# ------------------------------------------------------------------------------

def _run_sql_file(schema, sqlfile):
    """
        Run SQL file
            @param schema name of the target schema
            @param sqlfile name of the file to parse
    """
    # Run the SQL using DB command-line utility
    if portid in SUPPORTED_PORTS:
        sqlcmd = 'psql'
        # Test the DB cmd line utility
        std, err = subprocess.Popen(['which', sqlcmd], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE).communicate()
        if not std:
            error_(this, "Command not found: %s" % sqlcmd, True)

        runcmd = [sqlcmd, '-a',
                  '-v', 'ON_ERROR_STOP=1',
                  '-h', con_args['host'].split(':')[0],
                  '-p', con_args['host'].split(':')[1],
                  '-d', con_args['database'],
                  '-U', con_args['user'],
                  '--no-password',
                  '--single-transaction',
                  '-f', sqlfile]
        runenv = os.environ
        if 'password' in con_args:
            runenv["PGPASSWORD"] = con_args['password']
        runenv["PGOPTIONS"] = '-c client_min_messages=notice'

    # Open log file
    logfile = sqlfile + '.log'
    try:
        log = open(logfile, 'w')
    except:
        error_(this, "Cannot create log file: %s" % logfile, False)
        raise Exception

    # Run the SQL
    try:
        info_(this, "> ... executing " + sqlfile, verbose)
        info_(this, ' '.join(runcmd), verbose)
        retval = subprocess.call(runcmd, env=runenv, stdout=log, stderr=log)
    except:
        error_(this, "Failed executing %s" % sqlfile, False)
        raise Exception
    finally:
        log.close()
    # Check the exit status
    result = _parse_result_logfile(retval, logfile, sqlfile)
    return result
# ------------------------------------------------------------------------------

def _parse_result_logfile(retval, logfile, sql_abspath,
                          sql_filename=None, module=None, milliseconds=None):
    """
    Function to parse the logfile and return if its content indicate a failure
    or success.
    """
    is_install_check_logfile = bool(sql_filename and module)
    # Check the exit status
    if retval != 0:
        result = 'FAIL'
        global keeplogs
        keeplogs = True
    # Since every single statement in the test file gets logged,
    # an empty log file indicates an empty or a failed test
    elif os.path.isfile(logfile) and os.path.getsize(logfile) > 0:
        result = 'PASS'
    # Otherwise
    else:
        result = 'ERROR'

    if is_install_check_logfile:
        # Output result
        print "TEST CASE RESULT|Module: " + module + \
            "|" + os.path.basename(sql_filename) + "|" + result + \
            "|Time: %d milliseconds" % (milliseconds)

    if result == 'FAIL':
        info_(this, "Failed executing %s" % sql_abspath, True)
        info_(this, "Check the log at %s" % logfile, True)
    return result

def _check_db_port(portid):
    """
    Make sure we are connected to the expected DB platform
        @param portid expected DB port id - to be validates
    """
    # Postgres
    try:
        row = _internal_run_query("SELECT version() AS version", True)
    except:
        error_(this, "Cannot validate DB platform type", True)
    if row and row[0]['version'].lower().find(portid) >= 0:
        if portid == 'postgres':
            if row[0]['version'].lower().find('greenplum') < 0:
                return True
        elif portid == 'greenplum':
            return True
    return False
# ------------------------------------------------------------------------------


def _print_vers(new_madlib_ver, db_madlib_ver, con_args, schema):
    """
    Print version information
        @param new_madlib_ver OS-level MADlib version
        @param db_madlib_ver DB-level MADlib version
        @param con_args database connection arguments
        @param schema MADlib schema name
    """
    info_(this, "MADlib tools version    = %s (%s)" % (str(new_madlib_ver), sys.argv[0]), True)
    if con_args:
        try:
            info_(this, "MADlib database version = %s (host=%s, db=%s, schema=%s)"
                  % (db_madlib_ver, con_args['host'], con_args['database'], schema), True)
        except:
            info_(this, "MADlib database version = [Unknown] (host=%s, db=%s, schema=%s)"
                  % (db_madlib_ver, con_args['host'], con_args['database'], schema), True)
    return
# ------------------------------------------------------------------------------


def _plpy_check(py_min_ver):
    """
    Check pl/python existence and version
        @param py_min_ver min Python version to run MADlib
    """

    info_(this, "Testing PL/Python environment...", True)

    # Check PL/Python existence
    rv = _internal_run_query("SELECT count(*) AS CNT FROM pg_language "
                             "WHERE lanname = 'plpythonu'", True)
    if int(rv[0]['cnt']) > 0:
        info_(this, "> PL/Python already installed", verbose)
    else:
        info_(this, "> PL/Python not installed", verbose)
        info_(this, "> Creating language PL/Python...", True)
        try:
            _internal_run_query("CREATE LANGUAGE plpythonu;", True)
        except:
            error_(this, """Cannot create language plpythonu. Please check if you
                have configured and installed portid (your platform) with
                `--with-python` option. Stopping installation...""", False)
            raise Exception

    # Check PL/Python version
    _internal_run_query("DROP FUNCTION IF EXISTS plpy_version_for_madlib();", False)
    _internal_run_query("""
        CREATE OR REPLACE FUNCTION plpy_version_for_madlib()
        RETURNS TEXT AS
        $$
            import sys
            # return '.'.join(str(item) for item in sys.version_info[:3])
            return str(sys.version_info[:3]).replace(',','.').replace(' ','').replace(')','').replace('(','')
        $$
        LANGUAGE plpythonu;
    """, True)
    rv = _internal_run_query("SELECT plpy_version_for_madlib() AS ver;", True)
    python = rv[0]['ver']
    py_cur_ver = [int(i) for i in python.split('.')]
    if py_cur_ver >= py_min_ver:
        info_(this, "> PL/Python version: %s" % python, verbose)
    else:
        error_(this, "PL/Python version too old: %s. You need %s or greater"
               % (python, '.'.join(str(i) for i in py_min_ver)), False)
        raise Exception
    _internal_run_query("DROP FUNCTION IF EXISTS plpy_version_for_madlib();", False)
    info_(this, "> PL/Python environment OK (version: %s)" % python, True)
# ------------------------------------------------------------------------------


def _db_install(schema, is_schema_in_db, filehandle, testcase):
    """
    Install MADlib
        @param schema MADlib schema name
        @param is_schema_in_db flag to indicate if schema is already present
        @param filehandle file that contains the sql for installation
        @param testcase command-line args for a subset of modules
    """
    # Create MADlib objects
    try:
        _db_create_schema(schema, is_schema_in_db, filehandle)
        _db_create_objects(schema, filehandle, testcase=testcase)
    except:
        error_(this, "Building database objects failed. "
               "Before retrying: drop %s schema OR install MADlib into "
               "a different schema." % schema, True)

# ------------------------------------------------------------------------------


def _db_upgrade(schema, filehandle, db_madlib_ver):
    """
    Upgrade MADlib
        @param schema MADlib schema name
        @param filehandle Handle to output file
        @param db_madlib_ver DB-level MADlib version
    """
    if is_rev_gte(get_rev_num(db_madlib_ver), get_rev_num(new_madlib_ver)):
        info_(this, "Current MADlib version already up to date.", True)
        return 1

    if is_rev_gte(get_rev_num('1.9.1'), get_rev_num(db_madlib_ver)):
        error_(this, """
            MADlib versions prior to v1.10 are not supported for upgrade.
            Please try upgrading to v1.10 and then upgrade to this version.
            """, True)
        return 1

    info_(this, "Upgrading MADlib into %s schema..." % schema, True)
    info_(this, "\tDetecting dependencies...", True)

    info_(this, "\tLoading change list...", True)
    ch = uu.ChangeHandler(schema, portid, con_args, maddir, db_madlib_ver, filehandle)

    info_(this, "\tDetecting table dependencies...", True)
    td = uu.TableDependency(schema, portid, con_args)

    info_(this, "\tDetecting view dependencies...", True)
    vd = uu.ViewDependency(schema, portid, con_args)

    abort = False
    if td.has_dependency():
        info_(this, "*" * 50, True)
        info_(this, "\tFollowing user tables/indexes are dependent on MADlib objects:", True)
        info_(this, td.get_dependency_str(), True)
        info_(this, "*" * 50, True)
        cd_udt = [udt for udt in td.get_depended_udt() if udt in ch.udt]
        if len(cd_udt) > 0:
            error_(this, """
                User has objects dependent on following updated MADlib types!
                        {0}
                These objects need to be dropped before upgrading.
                """.format('\n\t\t\t'.join(cd_udt)), False)

            # we add special handling for 'linregr_result'
            if 'linregr_result' in cd_udt:
                info_(this, """Dependency on 'linregr_result' could be due to objects
                        created from the output of the aggregate 'linregr'.
                        Please refer to the Linear Regression documentation
                        <http://madlib.apache.org/docs/latest/group__grp__linreg.html#warning>
                        for the recommended solution.
                        """, False)
            abort = True

        c_udoc = ch.get_udoc_oids()
        d_udoc = td.get_depended_udoc_oids()
        cd_udoc = [udoc for udoc in d_udoc if udoc in c_udoc]
        if len(cd_udoc) > 0:
            error_(this, """
                User has objects dependent on the following updated MADlib operator classes!
                        oid={0}
                These objects need to be dropped before upgrading.
                """.format('\n\t\t\t'.join(cd_udoc)), False)
            abort = True

    if vd.has_dependency():
        info_(this, "*" * 50, True)
        info_(this, "\tFollowing user views are dependent on MADlib objects:", True)
        info_(this, vd.get_dependency_graph_str(), True)
        info_(this, "*" * 50, True)

        c_udf = ch.get_udf_signature()
        d_udf = vd.get_depended_func_signature('UDF')
        cd_udf = [udf for udf in d_udf if udf in c_udf]
        if len(cd_udf) > 0:
            error_(this, """
                User has objects dependent on following updated MADlib functions!
                    {0}
                These objects will fail to work with the updated functions and
                need to be dropped before starting upgrade again.
                """.format('\n\t\t\t\t\t'.join(cd_udf)), False)
            abort = True

        c_uda = ch.get_uda_signature()
        d_uda = vd.get_depended_func_signature('UDA')
        cd_uda = [uda for uda in d_uda if uda in c_uda]
        if len(cd_uda) > 0:
            error_(this, """
                User has objects dependent on following updated MADlib aggregates!
                    {0}
                These objects will fail to work with the new aggregates and
                need to be dropped before starting upgrade again.
                """.format('\n\t\t\t\t\t'.join(cd_uda)), False)
            abort = True

        c_udo = ch.get_udo_oids()
        d_udo = vd.get_depended_opr_oids()
        cd_udo = [udo for udo in d_udo if udo in c_udo]
        if len(cd_udo) > 0:
            error_(this, """
                User has objects dependent on following updated MADlib operators!
                    oid={0}
                These objects will fail to work with the new operators and
                need to be dropped before starting upgrade again.
                """.format('\n\t\t\t\t\t'.join(cd_udo)), False)
            abort = True

    if abort:
        error_(this, """------- Upgrade aborted. -------
                Backup and drop all objects that depend on MADlib before trying upgrade again.
                Use madpack reinstall to automatically drop these objects only if appropriate.""", True)
    else:
        info_(this, "No dependency problem found, continuing to upgrade ...", True)

    info_(this, "\tReading existing UDAs/UDTs...", False)
    sc = uu.ScriptCleaner(schema, portid, con_args, ch)
    info_(this, "Script Cleaner initialized ...", False)

    ch.drop_changed_uda()
    ch.drop_changed_udoc()
    ch.drop_changed_udo()
    ch.drop_changed_udc()
    ch.drop_changed_udf()
    ch.drop_changed_udt()  # assume dependent udf for udt does not change
    ch.drop_traininginfo_4dt()  # used types: oid, text, integer, float
    _db_create_objects(schema, filehandle, True, sc)
    return 0
# ------------------------------------------------------------------------------


def _db_rename_schema(from_schema, to_schema):
    """
    Rename schema
        @param from_schema name of the schema to rename
        @param to_schema new name for the schema
    """

    info_(this, "> Renaming schema %s to %s" % (from_schema, to_schema), True)
    try:
        _internal_run_query("ALTER SCHEMA %s RENAME TO %s;" % (from_schema, to_schema), True)
    except:
        error_(this, 'Cannot rename schema. Stopping installation...', False)
        raise Exception
# ------------------------------------------------------------------------------


def _db_create_schema(schema, is_schema_in_db, filehandle):
    """
    Create schema
        @param from_schema name of the schema to rename
        @param is_schema_in_db flag to indicate if schema is already present
        @param to_schema new name for the schema
    """

    if not is_schema_in_db:
        _write_to_file(filehandle, "CREATE SCHEMA %s;" % schema)
# ------------------------------------------------------------------------------

def _db_create_objects(schema, create_obj_handle,
                       upgrade=False, sc=None, testcase=""):
    """
    Create MADlib DB objects in the schema
        @param schema Name of the target schema
        @param create_obj_handle file handle for sql output file
        @param upgrade flag to indicate if it's an upgrade operation or not
        @param sc ScriptCleaner object
        @param testcase Command-line args for modules to install
    """
    if not upgrade:
        # Create MigrationHistory table
        try:
            _write_to_file(create_obj_handle,
                           "DROP TABLE IF EXISTS %s.migrationhistory;" % schema)
            sql = """CREATE TABLE %s.migrationhistory
                   (id serial, version varchar(255),
                    applied timestamp default current_timestamp);
                  """ % schema
            _write_to_file(create_obj_handle, sql)
        except:
            error_(this, "Cannot crate MigrationHistory table", False)
            raise Exception

    # Stamp the DB installation
    try:
        _write_to_file(create_obj_handle,
                       """INSERT INTO %s.migrationhistory(version)
                            VALUES('%s');
                       """ % (schema, str(new_madlib_ver)))
    except:
        error_(this, "Cannot insert data into %s.migrationhistory table" % schema, False)
        raise Exception

    # Run migration SQLs
    info_(this, "> Preparing objects for the following modules:", True)

    if testcase:
        caseset = set([test.strip() for test in testcase.split(',')])
    else:
        caseset = set()

    modset = {}
    for case in caseset:
        if case.find('/') > -1:
            [mod, algo] = case.split('/')
            if mod not in modset:
                modset[mod] = []
            if algo not in modset[mod]:
                modset[mod].append(algo)
        else:
            modset[case] = []

    # Loop through all modules/modules
    # portspecs is a global variable
    for moduleinfo in portspecs['modules']:

        # Get the module name
        module = moduleinfo['name']

        # Skip if doesn't meet specified modules
        if modset is not None and len(modset) > 0 and module not in modset:
            continue

        info_(this, "> - %s" % module, True)

        # Find the Python module dir (platform specific or generic)
        if os.path.isdir(maddir + "/ports/" + portid + "/" + dbver + "/modules/" + module):
            maddir_mod_py = maddir + "/ports/" + portid + "/" + dbver + "/modules"
        else:
            maddir_mod_py = maddir + "/modules"

        # Find the SQL module dir (platform specific or generic)
        if os.path.isdir(maddir + "/ports/" + portid + "/modules/" + module):
            maddir_mod_sql = maddir + "/ports/" + portid + "/modules"
        elif os.path.isdir(maddir + "/modules/" + module):
            maddir_mod_sql = maddir + "/modules"
        else:
            # This was a platform-specific module, for which no default exists.
            # We can just skip this module.
            continue

        # Make a temp dir for log files
        cur_tmpdir = tmpdir + "/" + module
        _make_dir(cur_tmpdir)

        # Loop through all SQL files for this module
        mask = maddir_mod_sql + '/' + module + '/*.sql_in'
        sql_files = glob.glob(mask)

        if not sql_files:
            error_(this, "No files found in: %s" % mask, True)

        # Execute all SQL files for the module
        for sqlfile in sql_files:
            algoname = os.path.basename(sqlfile).split('.')[0]
            # run only algo specified
            if module in modset and len(modset[module]) > 0 \
                    and algoname not in modset[module]:
                continue

            if not upgrade:
                _run_m4_and_append(schema, maddir_mod_py, module, sqlfile,
                                   create_obj_handle, None)
            else:
                tmpfile = cur_tmpdir + '/' + os.path.basename(sqlfile) + '.tmp'
                with open(tmpfile, 'w+') as tmphandle:
                    _run_m4_and_append(schema, maddir_mod_py, module, sqlfile,
                                       tmphandle, None)
                processed_sql = sc.cleanup(open(tmpfile).read(), algoname)
                _write_to_file(create_obj_handle, processed_sql)

# ------------------------------------------------------------------------------

def unescape(string):
    """
    Unescape separation characters in connection strings, i.e., remove first
    backslash from "\/", "\@", "\:", and "\\".
    """
    if string is None:
        return None
    else:
        return re.sub(r'\\(?P<char>[/@:\\])', '\g<char>', string)
# ------------------------------------------------------------------------------

def parseConnectionStr(connectionStr):
    """
    @brief Parse connection strings of the form
           <tt>[username[/password]@][hostname][:port][/database]</tt>

    Separation characters (/@:) and the backslash (\) need to be escaped.
    @returns A tuple (username, password, hostname, port, database). Field not
             specified will be None.
    """
    match = re.search(
        r'((?P<user>([^/@:\\]|\\/|\\@|\\:|\\\\)+)' +
        r'(/(?P<password>([^/@:\\]|\\/|\\@|\\:|\\\\)*))?@)?' +
        r'(?P<host>([^/@:\\]|\\/|\\@|\\:|\\\\)+)?' +
        r'(:(?P<port>[0-9]+))?' +
        r'(/(?P<database>([^/@:\\]|\\/|\\@|\\:|\\\\)+))?', connectionStr)
    return (
        unescape(match.group('user')),
        unescape(match.group('password')),
        unescape(match.group('host')),
        match.group('port'),
        unescape(match.group('database')))
# ------------------------------------------------------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="madpack",
        description='MADlib package manager (' + str(new_madlib_ver) + ')',
        argument_default=False,
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Example:

  $ madpack install -s madlib -p greenplum -c gpadmin@mdw:5432/testdb

  This will install MADlib objects into a Greenplum database called TESTDB
  running on server MDW:5432. Installer will try to login as GPADMIN
  and will prompt for password. The target schema will be MADLIB.
  """)

    help_msg = """One of the following options:
                  install        : run sql scripts to load into DB
                  upgrade        : run sql scripts to upgrade
                  uninstall      : run sql scripts to uninstall from DB
                  reinstall      : performs uninstall and install
                  version        : compare and print MADlib version (binaries vs database objects)
                  install-check  : test all installed modules
                  """
    choice_list = ['install', 'update', 'upgrade', 'uninstall',
                   'reinstall', 'version', 'install-check']

    parser.add_argument('command', metavar='COMMAND', nargs=1,
                        choices=choice_list, help=help_msg)

    parser.add_argument(
        '-c', '--conn', metavar='CONNSTR', nargs=1, dest='connstr', default=None,
        help="""Connection string of the following syntax:
                   [user[/password]@][host][:port][/database]
                 If not provided default values will be derived for PostgerSQL and Greenplum:
                 - user: PGUSER or USER env variable or OS username
                 - pass: PGPASSWORD env variable or runtime prompt
                 - host: PGHOST env variable or 'localhost'
                 - port: PGPORT env variable or '5432'
                 - db: PGDATABASE env variable or OS username""")

    parser.add_argument('-s', '--schema', nargs=1, dest='schema',
                        metavar='SCHEMA', default='madlib',
                        help="Target schema for the database objects.")

    parser.add_argument('-p', '--platform', nargs=1, dest='platform',
                        metavar='PLATFORM', choices=portid_list,
                        help="Target database platform, current choices: " + str(portid_list))

    parser.add_argument('-v', '--verbose', dest='verbose',
                        action="store_true", help="Verbose mode.")

    parser.add_argument('-l', '--keeplogs', dest='keeplogs', default=False,
                        action="store_true", help="Do not remove installation log files.")

    parser.add_argument('-d', '--tmpdir', dest='tmpdir', default='/tmp/',
                        help="Temporary directory location for installation log files.")

    parser.add_argument('-t', '--testcase', dest='testcase', default="",
                        help="Module names to test, comma separated. Effective only for install-check.")

    # Get the arguments
    return parser.parse_args()

def run_install_check(args, testcase):
    schema = args['schema']
    db_madlib_ver = args['db_madlib_ver']
    # 1) Compare OS and DB versions. Continue if OS = DB.
    if get_rev_num(db_madlib_ver) != get_rev_num(new_madlib_ver):
        _print_vers(new_madlib_ver, db_madlib_ver, con_args, schema)
        info_(this, "Versions do not match. Install-check stopped.", True)
        return

    # Create install-check user
    db_name = args["c_db"].replace('.', '').replace('-', '_')
    test_user = ('madlib_' +
                 new_madlib_ver.replace('.', '').replace('-', '_') +
                 '_installcheck_' + db_name)
    try:
        _internal_run_query("DROP USER IF EXISTS %s;" % (test_user), False)
    except:
        _internal_run_query("DROP OWNED BY %s CASCADE;" % (test_user), True)
        _internal_run_query("DROP USER IF EXISTS %s;" % (test_user), True)
    _internal_run_query("CREATE USER %s;" % (test_user), True)

    _internal_run_query("GRANT USAGE ON SCHEMA %s TO %s;" % (schema, test_user), True)

    # 2) Run test SQLs
    info_(this, "> Running test scripts for:", verbose)

    caseset = (set([test.strip() for test in testcase.split(',')])
               if testcase != "" else set())

    modset = {}
    for case in caseset:
        if case.find('/') > -1:
            [mod, algo] = case.split('/')
            if mod not in modset:
                modset[mod] = []
            if algo not in modset[mod]:
                modset[mod].append(algo)
        else:
            modset[case] = []

    # Loop through all modules
    try:
        for moduleinfo in portspecs['modules']:

            # Get module name
            module = moduleinfo['name']

            # Skip if doesn't meet specified modules
            if modset is not None and len(modset) > 0 and module not in modset:
                continue
            # JIRA: MADLIB-1078 fix
            # Skip pmml during install-check (when run without the -t option).
            # We can still run install-check on pmml with '-t' option.
            if not modset and module in ['pmml']:
                continue
            info_(this, "> - %s" % module, verbose)

            # Make a temp dir for this module (if doesn't exist)
            cur_tmpdir = tmpdir + '/' + module + '/test'  # tmpdir is a global variable
            _make_dir(cur_tmpdir)

            # Find the Python module dir (platform specific or generic)
            if os.path.isdir(maddir + "/ports/" + portid + "/" + dbver + "/modules/" + module):
                maddir_mod_py = maddir + "/ports/" + portid + "/" + dbver + "/modules"
            else:
                maddir_mod_py = maddir + "/modules"

            # Find the SQL module dir (platform specific or generic)
            if os.path.isdir(maddir + "/ports/" + portid + "/modules/" + module):
                maddir_mod_sql = maddir + "/ports/" + portid + "/modules"
            else:
                maddir_mod_sql = maddir + "/modules"

            # Prepare test schema
            test_schema = "madlib_installcheck_%s" % (module)
            _internal_run_query("DROP SCHEMA IF EXISTS %s CASCADE; CREATE SCHEMA %s;" %
                                (test_schema, test_schema), True)
            _internal_run_query("GRANT ALL ON SCHEMA %s TO %s;" %
                                (test_schema, test_user), True)

            # Switch to test user and prepare the search_path
            pre_sql = '-- Switch to test user:\n' \
                      'SET ROLE %s;\n' \
                      '-- Set SEARCH_PATH for install-check:\n' \
                      'SET search_path=%s,%s;\n' \
                      % (test_user, test_schema, schema)

            # Loop through all test SQL files for this module
            sql_files = maddir_mod_sql + '/' + module + '/test/*.sql_in'
            for sqlfile in sorted(glob.glob(sql_files), reverse=True):
                algoname = os.path.basename(sqlfile).split('.')[0]
                # run only algo specified
                if (module in modset and modset[module] and
                        algoname not in modset[module]):
                    continue

                # Set file names
                tmpfile = cur_tmpdir + '/' + os.path.basename(sqlfile) + '.tmp'
                logfile = cur_tmpdir + '/' + os.path.basename(sqlfile) + '.log'

                # If there is no problem with the SQL file
                milliseconds = 0

                # Run the SQL
                run_start = datetime.datetime.now()
                retval = _run_install_check_sql(schema, maddir_mod_py,
                                                module, sqlfile, tmpfile,
                                                logfile, pre_sql)
                # Runtime evaluation
                run_end = datetime.datetime.now()
                milliseconds = round((run_end - run_start).seconds * 1000 +
                                     (run_end - run_start).microseconds / 1000)

                # Check the exit status
                result = _parse_result_logfile(retval, logfile, tmpfile, sqlfile,
                                               module, milliseconds)
    finally:
        # Cleanup test schema for the module
        _internal_run_query("DROP SCHEMA IF EXISTS %s CASCADE;" % (test_schema), True)
        # Drop install-check user
        _internal_run_query("DROP OWNED BY %s CASCADE;" % (test_user), True)
        _internal_run_query("DROP USER %s;" % (test_user), True)

def _append_uninstall_madlib_sqlfile(schema, db_madlib_ver, is_schema_in_db,
                                     output_filehandle):
    if get_rev_num(db_madlib_ver) == [0]:
        info_(this, "Nothing to uninstall. No version found in schema %s." % schema, True)
        return 1, is_schema_in_db

    # Find any potential data to lose
    affected_objects = _internal_run_query("""
        SELECT
            n1.nspname AS schema,
            relname AS relation,
            attname AS column,
            typname AS type
        FROM
            pg_attribute a,
            pg_class c,
            pg_type t,
            pg_namespace n,
            pg_namespace n1
        WHERE
            n.nspname = '%s'
            AND t.typnamespace = n.oid
            AND a.atttypid = t.oid
            AND c.oid = a.attrelid
            AND c.relnamespace = n1.oid
            AND c.relkind = 'r'
        ORDER BY
            n1.nspname, relname, attname, typname""" % schema.lower(), True)

    info_(this, "*** Uninstalling MADlib ***", True)
    info_(this, "***********************************************************************************", True)
    info_(this, "* Schema %s and all database objects depending on it will be dropped!" % schema, True)
    if affected_objects:
        info_(this, "* If you continue the following data will be lost (schema : table.column : type):", True)
        for ao in affected_objects:
            info_(this, '* - ' + ao['schema'] + ' : ' + ao['relation'] + '.' +
                  ao['column'] + ' : ' + ao['type'], True)
    info_(this, "***********************************************************************************", True)
    info_(this, "Would you like to continue? [Y/N]", True)
    go = raw_input('>>> ').upper()
    while (go not in ('Y', 'N', 'YES', 'NO')):
        go = raw_input('Yes or No >>> ').upper()

    # 2) Do the uninstall/drop
    if go in ('N', 'NO'):
        info_(this, 'No problem. Nothing dropped.', True)
        return 1, is_schema_in_db
    elif go in ('Y', 'YES'):
        try:
            _write_to_file(output_filehandle,
                           "DROP SCHEMA %s CASCADE;" % (schema))
            is_schema_in_db = False
            return 0, is_schema_in_db
        except:
            error_(this, "Cannot drop schema %s." % schema, True)

    else:
        return 1, is_schema_in_db

def _append_install_madlib_sqlfile(schema, db_madlib_ver, is_schema_in_db,
                                   madpack_cmd, testcase, output_filehandle):
    # Refresh MADlib version in DB, None for GP/PG
    if madpack_cmd == 'reinstall':
        info_(this, "Setting MADlib database version to be None for reinstall", verbose)
        db_madlib_ver = None

    info_(this, "*** Installing MADlib ***", True)

    # 1) Compare OS and DB versions.
    # noop if OS <= DB.
    _print_vers(new_madlib_ver, db_madlib_ver, con_args, schema)

    if db_madlib_ver is None:
        # Case when there is no existing MADlib installation, proceed to create
        # objects if nothing installed in DB
        pass
    elif is_rev_gte(get_rev_num(db_madlib_ver), get_rev_num(new_madlib_ver)):
        # Case when existing MADlib version is the same/higher as the new installation.
        info_(this, "Current MADlib version already up to date.", True)
        return 1
    else:
        # Case when the existing MADlib installation is lower than the new
        # installation. Error out and refer to upgrade if OS > DB
        error_(this, """Aborting installation: existing MADlib version detected in {0} schema
                To upgrade the {0} schema to MADlib v{1} please run the following command:
                madpack upgrade -s {0} -p {2} [-c ...]
                """.format(schema, new_madlib_ver, portid), True)

    # 2) Run installation
    _plpy_check(py_min_ver)
    _db_install(schema, is_schema_in_db, output_filehandle,
                testcase)
    return 0

def create_install_madlib_sqlfile(args, madpack_cmd, testcase):
    upgrade = args['upgrade']
    schema = args['schema']
    db_madlib_ver = args['db_madlib_ver']
    is_schema_in_db = args['is_schema_in_db']
    return_signal = 0
    with open(args['output_filename'], 'a+') as output_filehandle:
        # COMMAND: uninstall/reinstall
        if madpack_cmd in ('uninstall', 'reinstall'):
            return_signal, is_schema_in_db = _append_uninstall_madlib_sqlfile(
                schema, db_madlib_ver, is_schema_in_db, output_filehandle)

        # COMMAND: install/reinstall
        if madpack_cmd in ('install', 'reinstall'):
            return_signal += _append_install_madlib_sqlfile(schema, db_madlib_ver,
                is_schema_in_db, madpack_cmd, testcase, output_filehandle)

        # COMMAND: upgrade
        if madpack_cmd in ('upgrade', 'update'):
            upgrade = True
            info_(this, "*** Upgrading MADlib ***", True)
            db_madlib_ver = get_db_madlib_version(con_args, schema)

            # 1) Check DB version. If None, nothing to upgrade.
            if not db_madlib_ver:
                info_(this, "MADlib is not installed in {schema} schema and there "
                      "is nothing to upgrade. Please use install "
                      "instead.".format(schema=schema),
                      True)
                return_signal += 1

            # 2) Compare OS and DB versions. Continue if OS > DB.
            _print_vers(new_madlib_ver, db_madlib_ver, con_args, schema)
            if is_rev_gte(get_rev_num(db_madlib_ver), get_rev_num(new_madlib_ver)):
                info_(this, "Current MADlib version is already up-to-date.", True)
                return_signal += 1

            # 3) Run upgrade
            _plpy_check(py_min_ver)
            return_signal = _db_upgrade(schema, output_filehandle, db_madlib_ver)

    return 1 if return_signal > 0 else 0

def main(argv):
    args = parse_arguments()

    global verbose
    verbose = args.verbose
    info_(this, "Arguments: " + str(args), verbose)
    global keeplogs
    keeplogs = args.keeplogs

    global tmpdir
    try:
        tmpdir = tempfile.mkdtemp('', 'madlib.', args.tmpdir)
    except OSError, e:
        tmpdir = e.filename
        error_(this, "cannot create temporary directory: '%s'." % tmpdir, True)

    # Parse SCHEMA
    if len(args.schema[0]) > 1:
        schema = args.schema[0].lower()
    else:
        schema = args.schema.lower()

    # Parse DB Platform (== PortID) and compare with Ports.yml
    global portid
    if args.platform:
        try:
            # Get the DB platform name == DB port id
            portid = args.platform[0].lower()
            ports[portid]
        except:
            portid = None
            error_(this, "Can not find specs for port %s" % (args.platform[0]), True)
    else:
        portid = None

    # Parse CONNSTR (only if PLATFORM and DBAPI2 are defined)
    if portid:
        connStr = "" if args.connstr is None else args.connstr[0]
        (c_user, c_pass, c_host, c_port, c_db) = parseConnectionStr(connStr)

        # Find the default values for PG and GP
        if portid in SUPPORTED_PORTS:
            if c_user is None:
                c_user = os.environ.get('PGUSER', getpass.getuser())
            if c_pass is None:
                c_pass = os.environ.get('PGPASSWORD', None)
            if c_host is None:
                c_host = os.environ.get('PGHOST', 'localhost')
            if c_port is None:
                c_port = os.environ.get('PGPORT', '5432')
            if c_db is None:
                c_db = os.environ.get('PGDATABASE', c_user)

        # Set connection variables
        global con_args
        con_args['host'] = c_host + ':' + c_port
        con_args['database'] = c_db
        con_args['user'] = c_user
        if c_pass is not None:
            con_args['password'] = c_pass

        # Try connecting to the database
        info_(this, "Testing database connection...", verbose)

        try:
            # check for password only if required
            _internal_run_query("SELECT 1", False)
        except EnvironmentError:
            con_args['password'] = getpass.getpass("Password for user %s: " % c_user)
            _internal_run_query("SELECT 1", False)
        except:
            error_(this, 'Failed to connect to database', True)

        # Get DB version
        global dbver
        dbver = get_dbver(con_args, portid)

        # update maddir to use a relative path if available
        global maddir
        maddir = _get_relative_maddir(maddir, portid)

        # Get MADlib version in DB
        db_madlib_ver = get_db_madlib_version(con_args, schema)

        portdir = os.path.join(maddir, "ports", portid)
        supportedVersions = [dirItem for dirItem in os.listdir(portdir)
                             if os.path.isdir(os.path.join(portdir, dirItem)) and
                             re.match("^\d+", dirItem)]
        if dbver is None:
            dbver = ".".join(
                map(str, max([versionStr.split('.')
                              for versionStr in supportedVersions])))
            info_(this, "Could not parse version string reported by {DBMS}. Will "
                  "default to newest supported version of {DBMS} "
                  "({version}).".format(DBMS=ports[portid]['name'],
                                        version=dbver), True)
        else:
            info_(this, "Detected %s version %s." % (ports[portid]['name'], dbver),
                  True)

            dbver_split = get_rev_num(dbver)
            if portid == 'greenplum':
                if is_rev_gte(dbver_split, get_rev_num('5.0')):
                    # GPDB (starting 5.0) uses semantic versioning. Hence, only
                    # need first digit for major version.
                    dbver = str(dbver_split[0])
                elif is_rev_gte(dbver_split, get_rev_num('4.3.5')):
                    # Due to the ABI incompatibility between 4.3.4 and 4.3.5,
                    # MADlib treats 4.3.5+ as DB version 4.3ORCA which is
                    # different from 4.3. The name is suffixed with ORCA since
                    # optimizer (ORCA) is 'on' by default in 4.3.5+
                    dbver = '4.3ORCA'
                else:
                    # only need the first two digits for <= 4.3.4
                    dbver = '.'.join(map(str, dbver_split[:2]))
            elif portid == 'postgres':
                if is_rev_gte(dbver_split, get_rev_num('10.0')):
                    # Postgres starting 10.0 uses semantic versioning. Hence,
                    # only need first digit for major version.
                    dbver = str(dbver_split[0])
            if not os.path.isdir(os.path.join(portdir, dbver)):
                error_(this, "This version is not among the %s versions for which "
                       "MADlib support files have been installed (%s)." %
                       (ports[portid]['name'], ", ".join(supportedVersions)), True)

        # Validate that db platform is correct
        if not _check_db_port(portid):
            error_(this, "Invalid database platform specified.", True)

        # Adjust MADlib directories for this port (if they exist)
        global maddir_conf
        if os.path.isdir(maddir + "/ports/" + portid + "/" + dbver + "/config"):
            maddir_conf = maddir + "/ports/" + portid + "/" + dbver + "/config"
        else:
            maddir_conf = maddir + "/config"

        global maddir_lib
        if os.path.isfile(maddir + "/ports/" + portid + "/" + dbver +
                          "/lib/libmadlib.so"):
            maddir_lib = maddir + "/ports/" + portid + "/" + dbver + \
                "/lib/libmadlib.so"
        else:
            maddir_lib = maddir + "/lib/libmadlib.so"

        # Get the list of modules for this port
        global portspecs
        portspecs = configyml.get_modules(maddir_conf)
    else:
        con_args = None
        db_madlib_ver = None

    # Parse COMMAND argument and compare with Ports.yml
    # Debugging...
    # print "OS new_madlib_ver: " + str(new_madlib_ver) + " > " + str(get_rev_num(new_madlib_ver))
    # print "DB new_madlib_ver: " + str(db_madlib_ver) + " > " + str(get_rev_num(db_madlib_ver))

    # Make sure we have the necessary parameters to continue
    if args.command[0] != 'version':
        if not portid:
            error_(this, "Missing -p/--platform parameter.", True)
        if not con_args:
            error_(this, "Unknown problem with database connection string: %s" % con_args, True)
    # ---------------- Completed "Get and validate arguments" -----------------

    # COMMAND: version
    if args.command[0] == 'version':
        _print_vers(new_madlib_ver, db_madlib_ver, con_args, schema)

    # COMMAND: install-check
    if args.command[0] == 'install-check':
        run_install_check(locals(), args.testcase)
    else:
        try:
            is_schema_in_db = _internal_run_query("SELECT schema_name FROM information_schema.schemata WHERE schema_name='%s';" % schema, True)
        except:
            error_(this, "Cannot validate if schema already exists.", True)

        output_filename = tmpdir + "/madlib_{0}.sql".format(args.command[0])
        upgrade = False
        return_val = create_install_madlib_sqlfile(locals(), args.command[0], args.testcase)
        if return_val == 0:
            op_msg = args.command[0].capitalize()+"ing" if args.command[0] != 'upgrade' \
                                                        else 'Upgrading'
            info_(this, "%s MADlib:" % op_msg, True)
            _cleanup_comments_in_sqlfile(output_filename, upgrade)
            result = _run_sql_file(schema, output_filename)

            if result == 'FAIL':
                info_(this, "MADlib {0} unsuccessful.".format(args.command[0]), True)
                info_(this, "All changes are rolled back.", True)
            else:
                if args.command[0] != 'uninstall':
                    if args.command[0] == 'upgrade':
                        info_(this, "MADlib %s upgraded successfully in %s schema." % (str(new_madlib_ver), schema), True)
                    else:
                        info_(this, "> Created %s schema" % schema, True)
                        info_(this, "> Created %s.MigrationHistory table" % schema, True)
                        info_(this, "> Wrote version info in MigrationHistory table", True)
                        info_(this, "MADlib %s installed successfully in %s schema." % (str(new_madlib_ver), schema))
                else :
                    info_(this, "MADlib %s uninstalled successfully from %s schema." % (str(new_madlib_ver), schema))



# ------------------------------------------------------------------------------
# Start Here
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    # Run main
    main(sys.argv[1:])

    # Optional log files cleanup
    # keeplogs and tmpdir are global variables
    if not keeplogs:
        shutil.rmtree(tmpdir)
    else:
        print "INFO: Log files saved in " + tmpdir
