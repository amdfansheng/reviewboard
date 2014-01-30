from __future__ import unicode_literals

import logging
import os
import re
import subprocess
import sys

from reviewboard.diffviewer.parser import DiffParser
from reviewboard.scmtools.core import SCMTool, HEAD, PRE_CREATION
from reviewboard.scmtools.errors import SCMError, FileNotFoundError

# This specific import is necessary to handle the paths for
# cygwin enabled machines.
if (sys.platform.startswith('win') or sys.platform.startswith('cygwin')):
    import ntpath as cpath
else:
    import posixpath as cpath

# This is a workaround for buggy Python 2.7.x and Windows 7.
# A console window would pop up every time popen is invoked unless shell=true.
# Original issue was described at http://reviews.reviewboard.org/r/3804/
# Note:
#   - later versions of Windows may probably be impacted too
#   - Python 2.7.x is the only one known to get this issue
import platform

if (sys.version_info[:2] == (2, 7) and
    platform.system() == "Windows" and
    platform.release() == "7"):
    _popen_shell = True
else:
    _popen_shell = False


class ClearCaseTool(SCMTool):
    name = 'ClearCase'
    uses_atomic_revisions = False
    supports_authentication = False
    field_help_text = {
        'path': 'The absolute path to the VOB.',
    }
    dependencies = {
        'executables': ['cleartool'],
    }

    # This regular expression can extract from extended_path
    # pure system path. It is construct from two main parts.
    # First match everything from beginning of line to first
    # occurence of /. Second match parts between /main and
    # numbers (file version).
    # This patch assume each branch present in extended_path
    # was derived from /main and there is no file or directory
    # called "main" in path.
    UNEXTENDED = re.compile(r'^(.+?)/|/?(.+?)/main/?.*?/([0-9]+|CHECKEDOUT)')

    VIEW_SNAPSHOT, VIEW_DYNAMIC, VIEW_UNKNOWN = range(3)

    def __init__(self, repository):
        self.repopath = repository.path

        SCMTool.__init__(self, repository)

        self.viewtype = self._get_view_type(self.repopath)

        if self.viewtype == self.VIEW_SNAPSHOT:
            self.client = ClearCaseSnapshotViewClient(self.repopath)
        elif self.viewtype == self.VIEW_DYNAMIC:
            self.client = ClearCaseDynamicViewClient(self.repopath)
        else:
            raise SCMError('Unsupported view type.')

    def unextend_path(self, extended_path):
        """Remove ClearCase revision and branch informations from path.

        ClearCase paths contain additional informations about branch
        and file version preceded by @@. This function remove this
        parts from ClearCase path to make it more readable
        For example this function convert extended path::

            /vobs/comm@@/main/122/network@@/main/55/sntp
            @@/main/4/src@@/main/1/sntp.c@@/main/8

        to the the to regular path::

            /vobs/comm/network/sntp/src/sntp.c
        """
        if not '@@' in extended_path:
            return HEAD, extended_path

        # Result of regular expression search result is list of tuples.
        # We must flat this to one list. The best way is use list comprehension.
        # b is first because it frequently occure in tuples.
        # Before that remove @@ from path.
        unextended_chunks = [
            b or a
            for a, b, foo in self.UNEXTENDED.findall(extended_path.replace('@@', ''))
        ]

        if sys.platform.startswith('win'):
            # Properly handle full (with drive letter) and UNC paths
            if unextended_chunks[0].endswith(':'):
                unextended_chunks[0] = '%s\\' % unextended_chunks[0]
            elif unextended_chunks[0] == '/' or unextended_chunks[0] == os.sep:
                unextended_chunks[0] = '\\\\'

        # Purpose of realpath is remove parts like /./ generated by
        # ClearCase when vobs branch was fresh created
        unextended_path = cpath.realpath(
            cpath.join(*unextended_chunks)
        )

        revision = extended_path.rsplit('@@', 1)[1]
        if revision.endswith('CHECKEDOUT'):
            revision = HEAD

        return (revision, unextended_path)

    @classmethod
    def relpath(cls, path, start):
        """Wrapper for os.path.relpath for Python 2.4.

        Python 2.4 doesn't have the os.path.relpath function, so this
        approximates it well enough for our needs.

        ntpath.relpath() overflows and throws TypeError for paths containing
        atleast 520 characters (not that hard to encounter in UCM
        repository).
        """
        try:
            return cpath.relpath(path, start)
        except (AttributeError, TypeError):
            if start[-1] != os.sep:
                start += os.sep

            return path[len(start):]

    def normalize_path_for_display(self, filename):
        """Return display friendly path without revision informations.

        In path construct for only display purpuse we don't need
        information about branch, version or even repository path
        so we return unextended path relative to repopath (view)
        """
        return self.relpath(self.unextend_path(filename)[1], self.repopath)

    def get_repository_info(self):
        vobstag = self._get_vobs_tag(self.repopath)
        return {
            'repopath': self.repopath,
            'uuid': self._get_vobs_uuid(vobstag)
        }

    def _get_view_type(self, repopath):
        cmdline = ["cleartool", "lsview", "-full", "-properties", "-cview"]
        p = subprocess.Popen(
            cmdline,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=repopath,
            shell=_popen_shell)

        (res, error) = p.communicate()
        failure = p.poll()

        if failure:
            raise SCMError(error)

        for line in res.splitlines(True):
            splitted = line.split(' ')
            if splitted[0] == 'Properties:':
                if 'snapshot' in splitted:
                    return self.VIEW_SNAPSHOT
                elif 'dynamic' in splitted:
                    return self.VIEW_DYNAMIC

        return self.VIEW_UNKNOWN

    def _get_vobs_tag(self, repopath):
        cmdline = ["cleartool", "describe", "-short", "vob:."]
        p = subprocess.Popen(
            cmdline,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.repopath,
            shell=_popen_shell)

        (res, error) = p.communicate()
        failure = p.poll()

        if failure:
            raise SCMError(error)

        return res.rstrip()

    def _get_vobs_uuid(self, vobstag):
        cmdline = ["cleartool", "lsvob", "-long", vobstag]
        p = subprocess.Popen(
            cmdline,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.repopath,
            shell=_popen_shell)

        (res, error) = p.communicate()
        failure = p.poll()

        if failure:
            raise SCMError(error)

        for line in res.splitlines(True):
            if line.startswith('Vob family uuid:'):
                return line.split(' ')[-1].rstrip()

        raise SCMError("Can't find familly uuid for vob: %s" % vobstag)

    def _get_object_kind(self, extended_path):
        cmdline = ["cleartool", "desc", "-fmt", "%m", extended_path]
        p = subprocess.Popen(
            cmdline,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.repopath,
            shell=_popen_shell)

        (res, error) = p.communicate()
        failure = p.poll()

        if failure:
            raise SCMError(error)

        return res.strip()

    def get_file(self, extended_path, revision=HEAD):
        """Return content of file or list content of directory"""
        if not extended_path:
            raise FileNotFoundError(extended_path, revision)

        if revision == PRE_CREATION:
            return ''

        if self.viewtype == self.VIEW_SNAPSHOT:
            # Get the path to (presumably) file element (remove version)
            # The '@@' at the end of file_path is required.
            file_path = extended_path.rsplit('@@', 1)[0] + '@@'
            okind = self._get_object_kind(file_path)

            if okind == 'directory element':
                raise SCMError('Directory elements are unsupported.')
            elif okind == 'file element':
                output = self.client.cat_file(extended_path, revision)
            else:
                raise FileNotFoundError(extended_path, revision)
        else:
            if cpath.isdir(extended_path):
                output = self.client.list_dir(extended_path, revision)
            elif cpath.exists(extended_path):
                output = self.client.cat_file(extended_path, revision)
            else:
                raise FileNotFoundError(extended_path, revision)

        return output

    def parse_diff_revision(self, extended_path, revision_str, *args, **kwargs):
        """Guess revision based on extended_path.

        Revision is part of file path, called extended-path,
        revision_str contains only modification's timestamp.
        """

        if extended_path.endswith(os.path.join(os.sep, 'main', '0')):
            revision = PRE_CREATION
        elif (extended_path.endswith('CHECKEDOUT')
              or not '@@' in extended_path):
            revision = HEAD
        else:
            revision = extended_path.rsplit('@@', 1)[1]

        return extended_path, revision

    def get_fields(self):
        return ['basedir', 'diff_path']

    def get_parser(self, data):
        return ClearCaseDiffParser(data,
                                   self.repopath,
                                   self._get_vobs_tag(self.repopath))


class ClearCaseDiffParser(DiffParser):
    """
    Special parsing for diffs created with the post-review for ClearCase.
    """

    SPECIAL_REGEX = re.compile(r'^==== (\S+) (\S+) ====$')

    def __init__(self, data, repopath, vobstag):
        self.repopath = repopath
        self.vobstag = vobstag
        super(ClearCaseDiffParser, self).__init__(data)

    def parse_diff_header(self, linenum, info):
        """Obtain correct clearcase file paths.

        Paths for the same file may differ from paths in developer view
        because it depends from configspec and this is custom so we
        translate oids, attached by post-review, to filenames to get paths
        working well inside clearcase view on reviewboard side.
        """

        # Because ==== oid oid ==== is present after each header
        # parse standard +++ and --- headers at the first place
        linenum = super(ClearCaseDiffParser, self).parse_diff_header(linenum, info)
        m = self.SPECIAL_REGEX.match(self.lines[linenum])

        if m:
            # When using ClearCase in multi-site mode, data replication takes
            # much time, including oid. As said above, oid is used to retrieve
            # filename path independent of developer view.
            # When an oid is not found on server side an exception is thrown
            # and review request submission fails.
            # However at this time origFile and newFile info have already been
            # filled by super.parse_diff_header and contain client side paths,
            # client side paths are enough to start reviewing.
            # So we can safely catch exception and restore client side paths
            # if not found.
            currentFilename = info['origFile']
            try:
                info['origFile'] = self._oid2filename(m.group(1))
            except:
                logging.debug("oid (%s) not found, get filename from client",
                              m.group(1))
                info['origFile'] = self.client_relpath(currentFilename)

            currentFilename = info['newFile']
            try:
               info['newFile'] = self._oid2filename(m.group(2))
            except:
                logging.debug("oid (%s) not found, get filename from client",
                              m.group(2))
                info['newFile'] = self.client_relpath(currentFilename)

            linenum += 1
            if (linenum < len(self.lines) and
                (self.lines[linenum].startswith("Binary files ") or
                 self.lines[linenum].startswith("Files "))):

                # To consider filenames translated from oids
                # origInfo and newInfo keys must exists.
                # Other files already contain this values field
                # by timestamp from +++/--- diff header.
                info['origInfo'] = ''
                info['newInfo'] = ''

                # Binary files need add origInfo and newInfo manally
                # because they don't have diff's headers (only oids).
                info['binary'] = True
                linenum += 1

        return linenum

    def _oid2filename(self, oid):
        cmdline = ["cleartool", "describe", "-fmt", "%En@@%Vn", "oid:%s" % oid]
        p = subprocess.Popen(
            cmdline,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.repopath,
            shell=_popen_shell)

        (res, error) = p.communicate()
        failure = p.poll()

        if failure:
            raise SCMError(error)

        drive = os.path.splitdrive(self.repopath)[0]
        if drive:
            res = os.path.join(drive, res)

        return ClearCaseTool.relpath(res, self.repopath)

    def client_relpath(self, filename):
        """Normalize any path sent from client view and return relative path
        against vobtag
        """
	path, revision = filename.split("@@", 1)
        relpath = ""
        logging.debug("vobstag: %s, path: %s", self.vobstag, path)
        while True:
            # An error should be raised if vobstag cannot be reached.
            if path == "/":
                logging.debug("vobstag not found in path, use client filename")
                return filename
            # Vobstag reach, relpath can be returned.
            if path.endswith(self.vobstag):
                break
            path, basename = os.path.split(path)
            # Init relpath with basename.
            if len(relpath) == 0:
                relpath = basename
            else:
                relpath = os.path.join(basename, relpath)

        logging.debug("relpath: %s", relpath)
        return relpath + "@@" + revision


class ClearCaseDynamicViewClient(object):
    def __init__(self, path):
        self.path = path

    def cat_file(self, filename, revision):
        with open(filename, 'rb') as f:
            return f.read()

    def list_dir(self, path, revision):
        return ''.join([
            '%s\n' % s
            for s in sorted(os.listdir(path))
        ])


class ClearCaseSnapshotViewClient(object):
    def __init__(self, path):
        self.path = path

    def cat_file(self, extended_path, revision):
        import tempfile
        # Use tempfile to generate temporary filename
        temp = tempfile.NamedTemporaryFile()
        # Remove the file, so cleartool can write to it
        temp.close()

        cmdline = ["cleartool", "get", "-to", temp.name, extended_path]
        p = subprocess.Popen(
            cmdline,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=_popen_shell)

        (res, error) = p.communicate()
        failure = p.poll()

        if failure:
            raise FileNotFoundError(extended_path, revision)

        try:
            with open(temp.name, 'rb') as f:
                return f.read()
        except:
            raise FileNotFoundError(extended_path, revision)
