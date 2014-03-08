import datetime
import sqlalchemy.orm.exc

from inbox.server import oauth
from inbox.server.pool import verify_yahoo_account
from inbox.server.models.tables import User, Namespace, ImapAccount

from inbox.server.auth.base import BaseAuth

PROVIDER = 'Yahoo'
IMAP_HOST = 'imap.mail.yahoo.com'


class YahooAuth(BaseAuth):
    @classmethod
    def auth_account(cls, email_address):
        return oauth.password_auth(email_address)

    @classmethod
    def compose_account(cls, db_session, email_address, response):
        try:
            account = db_session.query(ImapAccount).filter_by(
                email_address=email_address).one()
        except sqlalchemy.orm.exc.NoResultFound:
            user = User()
            namespace = Namespace()
            account = ImapAccount(user=user, namespace=namespace)

        account.provider = 'Yahoo'
        account.imap_host = IMAP_HOST
        account.email_address = response['email']
        account.password = response['password']
        account.date = datetime.datetime.utcnow()

        return account

    @classmethod
    def verify_account(cls, db_session, account):
        verify_yahoo_account(account)
        cls.commit_account(db_session, account)

        return account
