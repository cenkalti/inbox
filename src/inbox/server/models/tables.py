import os
import sys
import json
import traceback

from itertools import chain

from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Boolean
from sqlalchemy import ForeignKey, Enum, Text, Index, func, event
from sqlalchemy.orm import reconstructor, relationship, backref, deferred
from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound
from sqlalchemy.types import BLOB

from bs4 import BeautifulSoup, Doctype, Comment

from ..log import get_logger
log = get_logger()

from ..config import config
KEY_SIZE = int(config.get('KEY_SIZE', 128))
KEY_DIR = config.get('KEY_DIR', None)

from inbox.util.file import Lock, mkdirp
from inbox.util.html import plaintext2html
from inbox.util.misc import strip_plaintext_quote
from inbox.util.cryptography import encrypt_aes, decrypt_aes
from inbox.sqlalchemy.util import Base, JSON, LittleJSON
from inbox.sqlalchemy.revision import Revision, gen_rev_role
from inbox.server.oauth import AUTH_TYPES

from .roles import JSONSerializable, Blob

# global
class ImapAccount(Base):
    # user_id refers to Inbox's user id
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'),
            nullable=False)
    user = relationship("User", backref="imapaccounts")

    # http://stackoverflow.com/questions/386294/what-is-the-maximum-length-of-a-valid-email-address
    email_address = Column(String(254), nullable=True, index=True)
    provider = Column(Enum('Gmail', 'Outlook', 'Yahoo', 'Inbox'), nullable=False)
    imap_host = Column(String(512))

    # local flags & data
    save_raw_messages = Column(Boolean, default=True)

    sync_host = Column(String(255), nullable=True)
    last_synced_contacts = Column(DateTime, nullable=True)

    # folder name mappings for the Inbox datastore API
    inbox_folder_name = Column(String(255), nullable=True)
    drafts_folder_name = Column(String(255), nullable=True)
    # NOTE: Spam, Trash might be added later

    # Optional folder name mappings
    archive_folder_name = Column(String(255), nullable=True)
    sent_folder_name = Column(String(255), nullable=True)

    @property
    def sync_active(self):
        return self.sync_host is not None

    # oauth stuff (most providers support oauth at this point, shockingly)
    # TODO figure out the actual lengths of these
    # XXX we probably don't actually need to save all of this crap
    # XXX encrypt some of this crap?
    o_token_issued_to = Column(String(512))
    o_user_id = Column(String(512))
    o_access_token = Column(String(1024))
    o_id_token = Column(String(1024))
    o_expires_in = Column(Integer)
    o_access_type = Column(String(512))
    o_token_type = Column(String(512))
    o_audience = Column(String(512))
    o_scope = Column(String(512))
    o_refresh_token = Column(String(512))
    o_verified_email = Column(Boolean)

    # used to verify key lifespan
    date = Column(DateTime)

    # Password stuff
    # 'deferred' loads these large binary fields into memory only when needed
    # i.e. on direct access.
    password_aes = deferred(Column(BLOB(256)))
    key = deferred(Column(BLOB(128)))

    @property
    def _sync_lockfile_name(self):
        return "/var/lock/inbox_sync/{0}.lock".format(self.id)

    @property
    def _sync_lock(self):
        return Lock(self._sync_lockfile_name, block=False)

    def sync_lock(self):
        """ Prevent the mailsync for this account from running more than once.
        """
        self._sync_lock.acquire()

    def sync_unlock(self):
        self._sync_lock.release()

    @property
    def password(self):
        if self.password_aes is not None:
            keyfile = os.path.join(KEY_DIR, '{0}'.format(self.key))
            with open(keyfile, 'r') as f:
                key = f.read()

            key = self.key + key
            return decrypt_aes(self.password_aes, key)

    @password.setter
    def password(self, value):
        assert AUTH_TYPES.get(self.provider) == 'Password'
        assert value != None

        mkdirp(KEY_DIR)

        self.password_aes, key = encrypt_aes(value, KEY_SIZE)

        self.key = key[:len(key)/2]
        keyfile = os.path.join(KEY_DIR, '{0}'.format(self.key))

        with open(keyfile, 'w+') as f:
            f.write(key[len(key)/2:])

class UserSession(Base):
    """ Inbox-specific sessions. """
    token = Column(String(40))

    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'),
            nullable=False)
    user = relationship('User', backref='sessions')

class Namespace(Base):
    """ A way to do grouping / permissions, basically. """
    # NOTE: only root namespaces have IMAP accounts
    imapaccount_id = Column(Integer,
            ForeignKey('imapaccount.id', ondelete='CASCADE'), nullable=True)
    # really the root_namespace
    imapaccount = relationship('ImapAccount',
            backref=backref('namespace', uselist=False))

    # invariant: imapaccount is non-null iff type is root
    type = Column(Enum('root', 'shared_folder'), nullable=False, default='root')

    @property
    def email_address(self):
        if self.imapaccount is not None:
            return self.imapaccount.email_address

    def cereal(self):
        return dict(id=self.id, type=self.type)

class SharedFolder(Base):
    # Don't delete shared folders if the user that created them is deleted.
    user_id = Column(Integer, ForeignKey('user.id', ondelete='SET NULL'),
            nullable=True)
    user = relationship('User', backref='sharedfolders')

    namespace = relationship('Namespace', backref='sharedfolders')
    # Do delete shared folders if their associated namespace is deleted.
    namespace_id = Column(Integer, ForeignKey('namespace.id',
        ondelete='CASCADE'), nullable=False)

    display_name = Column(String(40))

    def cereal(self):
        return dict(id=self.id, name=self.display_name)

class User(Base):
    name = Column(String(255))

# sharded (by namespace)

class Transaction(Base, Revision):
    """ Transactional log to enable client syncing. """

    # Do delete transactions if their associated namespace is deleted.
    namespace_id = Column(Integer, ForeignKey('namespace.id',
        ondelete='CASCADE'), nullable=False)
    namespace = relationship('Namespace', backref='transactions')

    def set_extra_attrs(self, obj):
        try:
            self.namespace = obj.namespace
        except AttributeError:
            log.info("Couldn't create {2} revision for {0}:{1}".format(
                self.table_name, self.record_id, self.command))
            log.info("Delta is {0}".format(self.delta))
            log.info("Thread is: {0}".format(obj.thread_id))
            import pdb; pdb.set_trace()
            raise

HasRevisions = gen_rev_role(Transaction)

class Contact(Base, HasRevisions):
    """ Inbox-specific sessions. """
    imapaccount_id = Column(ForeignKey('imapaccount.id', ondelete='CASCADE'),
            nullable=False)
    imapaccount = relationship("ImapAccount")

    g_id = Column(String(64))
    source = Column("source", Enum("local", "remote"))

    email_address = Column(String(254), nullable=True, index=True)
    name = Column(Text)
    # phone_number = Column(String(64))

    updated_at = Column(DateTime, default=func.now(),
                        onupdate=func.current_timestamp())
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (UniqueConstraint('g_id', 'source', 'imapaccount_id'),)

    @property
    def namespace(self):
        return self.imapaccount.namespace

    def cereal(self):
        return dict(id=self.id,
                    email=self.email_address,
                    name=self.name)

    def __repr__(self):
        # XXX this won't work properly with unicode (e.g. in the name)
        return 'Contact({}, {}, {}, {})'.format(self.g_id, self.name,
            self.email_address, self.source)

class Message(JSONSerializable, Base, HasRevisions):
    # XXX clean this up a lot - make a better constructor, maybe taking
    # a flanker object as an argument to prefill a lot of attributes

    # Do delete messages if their associated thread is deleted.
    thread_id = Column(Integer, ForeignKey('thread.id', ondelete='CASCADE'),
            nullable=False)
    thread = relationship('Thread', backref="messages",
            order_by="Message.received_date")

    from_addr = Column(JSON, nullable=True)
    sender_addr = Column(JSON, nullable=True)
    reply_to = Column(JSON, nullable=True)
    to_addr = Column(JSON, nullable=True)
    cc_addr = Column(JSON, nullable=True)
    bcc_addr = Column(JSON, nullable=True)
    in_reply_to = Column(JSON, nullable=True)
    message_id_header = Column(String(255), nullable=True)
    subject = Column(Text, nullable=True)
    received_date = Column(DateTime, nullable=False)
    size = Column(Integer, default=0, nullable=False)
    data_sha256 = Column(String(255), nullable=True)

    mailing_list_headers = Column(JSON, nullable=True)

    is_draft = Column(Boolean, default=False, nullable=False)

    # Most messages are short and include a lot of quoted text. Preprocessing
    # just the relevant part out makes a big difference in how much data we
    # need to send over the wire.
    # Maximum length is determined by typical email size limits (25 MB body +
    # attachments on Gmail), assuming a maximum # of chars determined by
    # 1-byte (ASCII) chars.
    # NOTE: always HTML :)
    sanitized_body = Column(Text(length=26214400), nullable=False)
    snippet = Column(String(191), nullable=False)

    # we had to replace utf-8 errors before writing... this might be a
    # mail-parsing bug, or just a message from a bad client.
    decode_error = Column(Boolean, default=False, nullable=False)

    # only on messages from Gmail
    g_msgid = Column(BigInteger, nullable=True, index=True)
    g_thrid = Column(BigInteger, nullable=True, index=True)

    @property
    def namespace(self):
        return self.thread.namespace

    def calculate_sanitized_body(self):
        plain_part, html_part = self.body()
        snippet_length = 191
        if html_part:
            assert '\r' not in html_part, "newlines not normalized"

            # Rudimentary stripping out quoted text in 'gmail_quote' div
            # Wrap this in a try/catch because sometimes BeautifulSoup goes
            # down a dark spiral of recursion death
            try:
                soup = BeautifulSoup(html_part.strip(), "lxml")
                for div in soup.findAll('div', 'gmail_quote'):
                    div.extract()
                for container in soup.findAll('div', 'gmail_extra'):
                    if container.contents is not None:
                        for tag in reversed(container.contents):
                            if not hasattr(tag, 'name') or tag.name != 'br': break
                            else: tag.extract()
                    if container.contents is None:
                        # we emptied it!
                        container.extract()

                # Paragraphs don't need trailing line-breaks.
                for container in soup.findAll('p'):
                    if container.contents is not None:
                        for tag in reversed(container.contents):
                            if not hasattr(tag, 'name') or tag.name != 'br': break
                            else: tag.extract()

                # Misc other crap.
                dtd = [item for item in soup.contents if isinstance(item, Doctype)]
                comments = soup.findAll(text=lambda text:isinstance(text, Comment))
                for tag in chain(dtd, comments):
                    tag.extract()
                self.sanitized_body = unicode(soup)


                # trim for snippet
                for tag in soup.findAll(['style', 'head', 'title']):
                    tag.extract()
                self.snippet = soup.get_text(' ')[:191]


            except RuntimeError as exc:
                err_prefix = 'maximum recursion depth exceeded'
                # e.message is deprecated in Python 3
                if exc.args[0].startswith(err_prefix):
                    # err_snip = str(exc.args[0])[len(err_prefix):]
                    full_traceback = 'Ignoring error: %s\nOuter stack:\n%s%s' % \
                            (exc,
                            ''.join(traceback.format_stack()[:-2]), \
                            traceback.format_exc(exc))

                    # Note that python doesn't support tail call recursion optimizations
                    # http://neopythonic.blogspot.com/2009/04/tail-recursion-elimination.html
                    full_traceback = 'Error in BeautifulSoup.' + \
                                     'System recursion limit: {0}'.format(sys.getrecursionlimit()) + \
                                     '\n\n\n' + \
                                     full_traceback


                    # TODO have a better logging service for storing these
                    errdir = os.path.join(config['LOGDIR'], 'bs_parsing_errors', )
                    errfile = os.path.join(errdir, str(self.data_sha256))
                    mkdirp(errdir)

                    with open("{0}_traceback".format(errfile), 'w') as fh:
                        fh.write(full_traceback)
                    # Write the file in binary mode, since it might also have decoding errors.
                    with open("{0}_data".format(errfile), 'wb') as fh:
                        fh.write(html_part.encode("utf-8"))

                    log.error("BeautifulSoup parsing error. Data logged to {0}_data and {0}_traceback" \
                            .format(errfile))
                    self.decode_error = True

                    # Not sanitized, but will still work
                    self.sanitized_body = html_part
                    self.snippet = soup.get_text(' ')[:191]

                else:
                    log.error("Unknown BeautifulSoup exception: {0}".format(exc))
                    raise exc


        elif plain_part is None:
            self.sanitized_body = u''
            self.snippet = u''
        else:
            stripped = strip_plaintext_quote(plain_part.strip())
            self.sanitized_body = plaintext2html(stripped)
            self.snippet = stripped[:snippet_length]

    def body(self):
        """ Returns (plaintext, html) body for the message, decoded. """
        assert self.parts, \
                "Can't calculate body before parts have been parsed"

        plain_data = None
        html_data = None

        for part in self.parts:
            if part.content_type == 'text/html':
                html_data = part.get_data().decode('utf-8')
                break
        for part in self.parts:
            if part.content_type == 'text/plain':
                plain_data = part.get_data().decode('utf-8')
                break

        return plain_data, html_data

    def trimmed_subject(self):
        s = self.subject
        if s[:4] == u'RE: ' or s[:4] == u'Re: ' :
            s = s[4:]
        return s

    @property
    def prettified_body(self):
        html_data = self.sanitized_body

        prettified = None
        if 'font:' in html_data or 'font-face:' \
                in html_data or 'font-family:' in html_data:
            prettified = html_data
        else:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "message_template.html")
            with open(path, 'r') as f:
                # template has %s in it. can't do format because python
                # misinterprets css
                prettified = f.read() % html_data

        return prettified

    def cereal(self):
        # TODO serialize more here for client API
        d = {}
        d['from'] = self.from_addr
        d['to'] = self.to_addr
        d['date'] = self.received_date
        d['subject'] = self.subject
        d['id'] = self.id
        d['thread_id'] = self.thread_id
        d['snippet'] = self.snippet
        d['body'] = self.prettified_body
        d['mailing_list_info'] = self.mailing_list_headers
        return d

    @property
    def mailing_list_info(self):
        return self.mailing_list_headers

    @property
    def headers(self):
        """ Returns headers for the message, decoded. """
        assert self.parts, \
                "Can't provide headers before parts have been parsed"

        headers = self.parts[0].get_data()
        json_headers = json.JSONDecoder().decode(headers)

        return json_headers

# These are the top 15 most common Content-Type headers
# in my personal mail archive. --mg
common_content_types = ['text/plain',
                        'text/html',
                        'multipart/alternative',
                        'multipart/mixed',
                        'image/jpeg',
                        'multipart/related',
                        'application/pdf',
                        'image/png',
                        'image/gif',
                        'application/octet-stream',
                        'multipart/signed',
                        'application/msword',
                        'application/pkcs7-signature',
                        'message/rfc822',
                        'image/jpg']

class Block(JSONSerializable, Blob, Base, HasRevisions):
    """ Metadata for message parts stored in s3 """
    message_id = Column(Integer, ForeignKey('message.id', ondelete='CASCADE'),
            nullable=False)
    message = relationship('Message',
            backref=backref("parts", cascade="all, delete, delete-orphan"))

    walk_index = Column(Integer)
    # Save some space with common content types
    _content_type_common = Column(Enum(*common_content_types))
    _content_type_other = Column(String(255))
    filename = Column(String(255))

    content_disposition = Column(Enum('inline', 'attachment'))
    content_id = Column(String(255))  # For attachments
    misc_keyval = Column(JSON)

    is_inboxapp_attachment = Column(Boolean, default=False)

    # TODO: create a constructor that allows the 'content_type' keyword

    __table_args__ = (UniqueConstraint('message_id', 'walk_index', 'data_sha256'),)

    def __init__(self, *args, **kwargs):
        self.content_type = None
        self.size = 0
        Base.__init__(self, *args, **kwargs)

    def __repr__(self):
        return 'Block: %s' % self.__dict__

    def client_json(self):
        d = {}
        d['g_id'] = self.message.g_msgid
        d['g_index'] = self.walk_index
        d['content_type'] = self.content_type
        d['content_disposition'] = self.content_disposition
        d['size'] = self.size
        d['filename'] = self.filename
        return d

    @reconstructor
    def init_on_load(self):
        if self._content_type_common:
            self.content_type = self._content_type_common
        else:
            self.content_type = self._content_type_other

    @property
    def namespace(self):
        return self.message.namespace

@event.listens_for(Block, 'before_insert', propagate = True)
def serialize_before_insert(mapper, connection, target):
    if target.content_type in common_content_types:
        target._content_type_common = target.content_type
        target._content_type_other = None
    else:
        target._content_type_common = None
        target._content_type_other = target.content_type

class FolderItem(JSONSerializable, Base, HasRevisions):
    """ Maps threads to folders.

    Threads in this table are the _Inbox_ datastore abstraction, which may
    be different from folder names in the actual account backends.
    """
    thread_id = Column(Integer, ForeignKey('thread.id', ondelete='CASCADE'),
            nullable=False)
    # thread relationship is on Thread to make delete-orphan cascade work

    folder_name = Column(String(191), index=True)

    @property
    def namespace(self):
        return self.thread.namespace

    __table_args__ = (UniqueConstraint('folder_name', 'thread_id'),)

class ImapUid(JSONSerializable, Base):
    """ This maps UIDs to the IMAP folder they belong to, and extra metadata
        such as flags.

        This table is used solely for bookkeeping by the IMAP mail sync
        backends.
    """
    imapaccount_id = Column(ForeignKey('imapaccount.id', ondelete='CASCADE'),
            nullable=False)
    imapaccount = relationship("ImapAccount")
    # If we delete this uid, we also want the associated message to be deleted.
    # Buf if we delete the message, we _don't_ always want to delete the
    # associated uid, we want to be explicit about that (this makes the
    # local data/remote data synchronization work properly). this is why we
    # do not specify the "delete-orphan" cascade option here.
    message_id = Column(Integer, ForeignKey('message.id'), nullable=True)
    message = relationship('Message', cascade="all",
            backref=backref('imapuid', uselist=False))
    # nullable to allow the local data store to delete messages without
    # deleting the associated uid; we want to leave the uid entry there until
    # we notice the same delete from the backend, which helps our accounting
    # of what is going on. otherwise, it wouldn't make sense to allow these
    # entries to decouple.
    msg_uid = Column(Integer, nullable=True)

    # maximum Gmail label length is 225 (tested empirically), but constraining
    # folder_name uniquely requires max length of 767 bytes under utf8mb4
    # http://mathiasbynens.be/notes/mysql-utf8mb4
    folder_name = Column(String(191))

    ### Flags ###
    # Message has not completed composition (marked as a draft).
    is_draft = Column(Boolean, default=False, nullable=False)
    # Message has been read
    is_seen = Column(Boolean, default=False, nullable=False)
    # Message is "flagged" for urgent/special attention
    is_flagged = Column(Boolean, default=False, nullable=False)
    # session is the first session to have been notified about this message
    is_recent = Column(Boolean, default=False, nullable=False)
    # Message has been answered
    is_answered = Column(Boolean, default=False, nullable=False)
    # things like: ['$Forwarded', 'nonjunk', 'Junk']
    extra_flags = Column(LittleJSON, nullable=False)

    def update_flags(self, new_flags, x_gm_labels=None):
        new_flags = set(new_flags)
        col_for_flag = {
                u'\\Draft': 'is_draft',
                u'\\Seen': 'is_seen',
                u'\\Recent': 'is_recent',
                u'\\Answered': 'is_answered',
                u'\\Flagged': 'is_flagged',
                }
        for flag, col in col_for_flag.iteritems():
            setattr(self, col, flag in new_flags)
            new_flags.discard(flag)
        # Gmail doesn't use the \Draft flag. Go figure.
        if x_gm_labels is not None and '\\Draft' in x_gm_labels:
            self.is_draft = True
        self.extra_flags = sorted(new_flags)

    @property
    def namespace(self):
        return self.imapaccount.namespace

    __table_args__ = (UniqueConstraint('folder_name', 'msg_uid', 'imapaccount_id',),)

# make pulling up all messages in a given folder fast
Index('imapuid_imapaccount_id_folder_name', ImapUid.imapaccount_id,
        ImapUid.folder_name)

class UIDValidity(JSONSerializable, Base):
    """ UIDValidity has a per-folder value. If it changes, we need to
        re-map g_msgid to UID for that folder.
    """
    imapaccount_id = Column(ForeignKey('imapaccount.id', ondelete='CASCADE'),
            nullable=False)
    imapaccount = relationship("ImapAccount")
    # maximum Gmail label length is 225 (tested empirically), but constraining
    # folder_name uniquely requires max length of 767 bytes under utf8mb4
    # http://mathiasbynens.be/notes/mysql-utf8mb4
    folder_name = Column(String(191), nullable=False)
    uid_validity = Column(Integer, nullable=False)
    # invariant: the local datastore for this folder has always incorporated
    # remote changes up to _at least_ this modseq (we can't guarantee that
    # we haven't incorporated later changes too, since IMAP doesn't provide
    # a true transactional interface)
    highestmodseq = Column(Integer, nullable=False)

    __table_args__ = (UniqueConstraint('imapaccount_id', 'folder_name'),)

class Thread(JSONSerializable, Base):
    """ Threads are a first-class object in Inbox. This thread aggregates
        the relevant thread metadata from elsewhere so that clients can only
        query on threads.

        A thread can be a member of an arbitrary number of folders.

        If you're attempting to display _all_ messages a la Gmail's All Mail,
        don't query based on folder!
    """
    subject = Column(Text, nullable=True)
    subjectdate = Column(DateTime, nullable=False)
    recentdate = Column(DateTime, nullable=False)

    folders = relationship('FolderItem', backref="thread", single_parent=True,
            order_by="FolderItem.folder_name",
            cascade='all, delete, delete-orphan')

    namespace_id = Column(ForeignKey('namespace.id', ondelete='CASCADE'),
            nullable=False, index=True)
    namespace = relationship('Namespace', backref='threads')

    # only on messages from Gmail
    # NOTE: The same message sent to multiple users will be given a
    # different g_thrid for each user. We don't know yet if g_thrids are
    # unique globally.

    # Gmail documents X-GM-THRID as 64-bit unsigned integer
    g_thrid = Column(BigInteger, nullable=True, index=True)

    mailing_list_headers = Column(JSON, nullable=True)

    def update_from_message(self, message):
        if message.received_date > self.recentdate:
            self.recentdate = message.received_date
        # subject is subject of original message in the thread
        if message.received_date < self.recentdate:
            self.subject = message.subject
            self.subjectdate = message.received_date

        if len(message.mailing_list_headers) > len(self.mailing_list_headers):
            self.mailing_list_headers = message.mailing_list_headers
        return self

    @classmethod
    def from_message(cls, session, namespace, message):
        """
        Threads are broken solely on Gmail's X-GM-THRID for now. (Subjects
        are not taken into account, even if they change.)

        Returns the updated or new thread, and adds the message to the thread.
        Doesn't commit.
        """
        try:
            thread = session.query(cls).filter_by(g_thrid=message.g_thrid,
                    namespace=namespace).one()
            return thread.update_from_message(message)
        except NoResultFound:
            pass
        except MultipleResultsFound:
            log.info("Duplicate thread rows for thread {0}".format(message.g_thrid))
            raise
        thread = cls(subject=message.subject, g_thrid=message.g_thrid,
                recentdate=message.received_date, namespace=namespace,
                subjectdate=message.received_date,
                mailing_list_headers=message.mailing_list_headers)
        return thread

    @property
    def mailing_list_info(self):
        return self.mailing_list_headers

    def is_mailing_list_thread(self):
        for v in self.mailing_list_headers.itervalues():
            if (v != None):
                return True
        return False

    def cereal(self):
        """ Threads are serialized with full message data. """
        d = {}
        d['id'] = self.id
        d['messages'] = [m.cereal() for m in self.messages]
        d['subject'] = self.subject
        d['recentdate'] = self.recentdate
        return d

class FolderSync(Base):
    imapaccount_id = Column(ForeignKey('imapaccount.id', ondelete='CASCADE'),
            nullable=False)
    imapaccount = relationship('ImapAccount', backref='foldersyncs')
    # maximum Gmail label length is 225 (tested empirically), but constraining
    # folder_name uniquely requires max length of 767 bytes under utf8mb4
    # http://mathiasbynens.be/notes/mysql-utf8mb4
    folder_name = Column(String(191), nullable=False)

    # see state machine in mailsync/imap.py
    state = Column(Enum('initial', 'initial uidinvalid',
                        'poll', 'poll uidinvalid', 'finish'),
                        default='initial', nullable=False)

    __table_args__ = (UniqueConstraint('imapaccount_id', 'folder_name'),)
