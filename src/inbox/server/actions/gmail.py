""" Operations for syncing back local datastore changes to Gmail.

For IMAP, we could guarantee correctness with a full bidirectional sync by
using a conservative algorithm like OfflineIMAP's
(http://offlineimap.org/howitworks.html), but doing so wouldn't take advantage
of newer IMAP extensions like CONDSTORE that make us have to do much less
comparison and bookkeeping work.

We don't get any notion of "transactions" from the remote with IMAP. Here are
the possible cases for IMAP message changes:

* new
  - This message is either new-new and needs to be synced to us, or it's a
    "sent" or "draft" message and we need to check whether or not we have it,
    since we may have already saved a local copy. If we do already have it,
    we need to make a new ImapUid for it and associate the Message object with
    its ImapUid.
* changed
  - Update our flags or do nothing if the message isn't present locally. (NOTE:
    this could mean the message has been moved locally, in which case we will
    LOSE the flag change. We can fix this case in an eventually consistent
    manner by sanchecking flags on all messages in an account once a day or
    so.)
* delete
  - We always figure this out by comparing message lists against the local
    repo. Since we're using the mailsync-specific ImapUid objects for
    comparison, we automatically exclude Inbox-local sent and draft messages
    from this calculation.

We don't currently handle these operations on the special folders 'junk',
'trash', 'sent', 'flagged'.
"""
from ..crispin import new_crispin

from ..models import session_scope
from ..models.tables import ImapAccount, Namespace, Thread

class ActionError(Exception): pass

def uidvalidity_cb(db_session, account_id):
    """ Gmail Syncback actions never ever touch the database and don't rely on
        local UIDs since they instead do SEARCHes based on X-GM-THRID to find
        the message UIDs to act on. So we don't actually need to care about
        UIDVALIDITY.
    """
    pass

def _translate_folder_name(inbox_folder_name, crispin_client, c):
    """
    Folder names are *Inbox* datastore folder names; it's the responsibility
    of syncback API functions to translate that back to the proper Gmail folder
    name.
    """
    if inbox_folder_name in crispin_client.folder_names(c)['labels']:
        return inbox_folder_name
    elif inbox_folder_name in crispin_client.folder_names(c):
        return crispin_client.folder_names(c)[inbox_folder_name]
    else:
        raise Exception("weird Gmail folder that's not special or a label: {0}".format(inbox_folder_name))

def _syncback_action(fn, imapaccount_id, folder_name):
    """ `folder_name` is an Inbox folder name, not a Gmail folder name. """
    with session_scope() as db_session:
        account = db_session.query(ImapAccount).join(Namespace).filter_by(
                id=imapaccount_id).one()
        crispin_client = new_crispin(account.id, account.provider,
                conn_pool_size=1, readonly=False)
        with crispin_client.pool.get() as c:
            crispin_client.select_folder(
                    _translate_folder_name(folder_name, crispin_client, c),
                    uidvalidity_cb, c)
            fn(account, db_session, crispin_client, c)

def _archive(g_thrid, crispin_client, c):
    assert crispin_client.selected_folder_name \
            == crispin_client.folder_names(c)['inbox'], "must select inbox first"
    crispin_client.archive_thread(g_thrid, c)

def _get_g_thrid(namespace_id, thread_id, db_session):
    return db_session.query(Thread.g_thrid).filter_by(
            namespace_id=namespace_id,
            id=thread_id).one()[0]

def archive(imapaccount_id, thread_id):
    def fn(account, db_session, crispin_client, c):
        g_thrid = _get_g_thrid(account.namespace.id, thread_id, db_session)
        return _archive(g_thrid, crispin_client, c)

    return _syncback_action(fn, imapaccount_id, 'inbox')

def move(imapaccount_id, thread_id, from_folder, to_folder):
    if from_folder == to_folder:
        return

    def fn(account, db_session, crispin_client, c):
        if from_folder == 'inbox':
            if to_folder == 'archive':
                return _archive(thread_id, crispin_client, c)
            else:
                g_thrid = _get_g_thrid(account.namespace.id, thread_id,
                        db_session)
                _archive(g_thrid, crispin_client, c)
                crispin_client.add_label(g_thrid,
                        _translate_folder_name(to_folder, crispin_client, c), c)
        elif from_folder in crispin_client.folder_names(c)['labels']:
            if to_folder in crispin_client.folder_names(c)['labels']:
                g_thrid = _get_g_thrid(account.namespace.id, thread_id,
                        db_session)
                crispin_client.add_label(g_thrid,
                        _translate_folder_name(to_folder, crispin_client, c),
                        c)
                crispin_client.select_folder(
                        crispin_client.folder_names(c)['all'],
                        uidvalidity_cb, c)
                crispin_client.remove_label(g_thrid,
                        _translate_folder_name(from_folder, crispin_client, c),
                        c)
            elif to_folder == 'inbox':
                g_thrid = _get_g_thrid(account.namespace.id, thread_id,
                        db_session)
                crispin_client.copy_thread(g_thrid,
                        _translate_folder_name(to_folder, crispin_client, c),
                        c)
            elif to_folder != 'archive':
                raise Exception("Should never get here! to_folder: {0}" \
                        .format(to_folder))
            # do nothing if moving to all mail
        elif from_folder == 'archive':
            g_thrid = _get_g_thrid(account.namespace.id, thread_id, db_session)
            if to_folder in crispin_client.folder_names(c)['labels']:
                crispin_client.add_label(g_thrid,
                        _translate_folder_name(to_folder, crispin_client, c),
                        c)
            elif to_folder == 'inbox':
                crispin_client.copy_thread(g_thrid,
                        _translate_folder_name(to_folder, crispin_client, c),
                        c)
            else:
                raise Exception("Should never get here! to_folder: {0}".format(to_folder))
        else:
            raise Exception("Unknown from_folder '{0}'".format(from_folder))

    return _syncback_action(fn, imapaccount_id, from_folder)

def copy(imapaccount_id, thread_id, from_folder, to_folder):
    if from_folder == to_folder:
        return

    def fn(account, db_session, crispin_client, c):
        g_thrid = _get_g_thrid(account.namespace.id, thread_id, db_session)
        if to_folder == 'inbox':
            crispin_client.copy_thread(g_thrid,
                    _translate_folder_name(to_folder, crispin_client, c), c)
        elif to_folder != 'archive':
            crispin_client.add_label(g_thrid,
                    _translate_folder_name(to_folder, crispin_client, c), c)
        # copy a thread to all mail is a noop

    return _syncback_action(fn, imapaccount_id, from_folder)

def delete(imapaccount_id, thread_id, folder_name):
    def fn(account, db_session, crispin_client, c):
        g_thrid = _get_g_thrid(account.namespace.id, thread_id, db_session)
        if folder_name == 'inbox':
            return _archive(g_thrid, crispin_client, c)
        elif folder_name in crispin_client.folder_names(c)['labels']:
            crispin_client.select_folder(
                    crispin_client.folder_names(c)['all'], uidvalidity_cb, c)
            crispin_client.remove_label(g_thrid,
                    _translate_folder_name(folder_name, crispin_client, c), c)
        elif folder_name == 'archive':
            # delete thread from all mail: really delete it (move it to trash
            # where it will be permanently deleted after 30 days, see
            # https://support.google.com/mail/answer/78755?hl=en)
            # XXX: does copy() work here, or do we have to actually _move_
            # the message? do we also need to delete it from all labels and
            # stuff? not sure how this works really.
            crispin_client.copy_thread(g_thrid,
                    crispin_client.folder_names(c)['trash'], c)
        else:
            raise Exception("Unknown folder_name '{0}'".format(folder_name))

    return _syncback_action(fn, imapaccount_id, folder_name)
