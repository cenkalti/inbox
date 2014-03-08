import os, sys

from inbox.server.log import get_logger
log = get_logger()

from inbox.util.url import provider_from_address


class BaseAuth(object):
    AUTH_CLS_FOR = {}

    @classmethod
    def get_handler(cls, email_address):
        cls.register_backends()

        provider = provider_from_address(email_address)
        auth_cls = cls.AUTH_CLS_FOR.get(provider)

        if auth_cls is None:
            raise NotSupportedError('Inbox currently only supports Gmail and Yahoo.')
            sys.exit(1)

        return auth_cls

    @classmethod
    def create_account(cls, db_session, email_address):
        response = cls.auth_account(email_address)
        account = cls.compose_account(db_session, email_address, response)

        return account

    @classmethod
    def auth_account(cls, email_address):
        raise NotImplementedError

    @classmethod
    def compose_account(cls, db_session, email_address, response):
        raise NotImplementedError

    @classmethod
    def verify_account(cls, db_session, account):
        raise NotImplementedError

    @classmethod
    def commit_account(cls, db_session, account):
        db_session.add(account)
        db_session.commit()

        log.info("Stored new account {0}".format(account.email_address))

    @classmethod
    def register_backends(cls):
        """
        Finds the auth modules for the different providers
        (in the backends directory) and imports them.

        Creates a mapping of provider:auth_cls for each backend found.
        """
        # Find and import
        backend_dir = os.path.dirname(os.path.realpath(__file__))
        backend_files = [x[:-3] for x in os.listdir(backend_dir) if x.endswith('.py')]

        sys.path.insert(1, backend_dir)

        modules = [__import__(backend) for backend in backend_files]

        # Create mapping
        for module in modules:
            if getattr(module, 'PROVIDER', None) is not None:
                provider = module.PROVIDER
                auth_cls = getattr(module, '%sAuth' % provider)

                cls.AUTH_CLS_FOR[provider] = auth_cls
