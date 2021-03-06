import datetime
import logging
import os
import uuid

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import gnupg


class CertProcessorKeyNotFoundError(Exception):
    pass


class CertProcessorInvalidSignatureError(Exception):
    pass


class CertProcessor:
    def __init__(self, config):
        gnupg_path = config.get('mtls', 'gnupg_home')
        if not os.path.isabs(gnupg_path):
            gnupg_path = os.path.abspath(
                os.path.join(
                    os.path.dirname(__file__), gnupg_path
                )
            )
        self.gpg = gnupg.GPG(gnupghome=gnupg_path)
        self.gpg.encoding = 'utf-8'
        self.config = config
        self.openssl_format = serialization.PrivateFormat.TraditionalOpenSSL
        self.no_encyption = serialization.NoEncryption()

    def encrypt(self, data, recipients, sign=False):
        return self.gpg.encrypt(data, recipients, sign=sign)

    def decrypt(self, data):
        fingerprint = None
        try:
            data = self.gpg.decrypt(data)
            fingerprint = data.fingerprint
        except Exception as e:
            logging.error(e)
            data = None
        if data.ok is False:
            logging.error(data.status)
            data = None
        return data, fingerprint

    def verify(self, csr, signature):
        verified = self.gpg.verify_data(signature,
                                        csr)
        if not verified.valid:
            logging.error(str(verified))
            raise CertProcessorInvalidSignatureError

    def get_csr(self, csr):
        try:
            return x509.load_pem_x509_csr(bytes(csr, 'utf-8'),
                                          default_backend())
        except Exception as e:
            logging.error(e)
            return None

    def get_ca_key(self):
        ca_key_path = self.config.get('mtls', 'ca_key')
        if not os.path.isabs(ca_key_path):
            ca_key_path = os.path.abspath(
                os.path.join(
                    os.path.dirname(__file__),
                    ca_key_path
                )
            )
        try:
            with open(ca_key_path, 'rb') as key_file:
                ca_key = serialization.load_pem_private_key(
                    key_file.read(),
                    password=None,
                    backend=default_backend()
                )
                return ca_key
        except ValueError:
            logging.error('Erroring opening file: {}'.format(ca_key_path))
            logging.error('Generating new key...')
            key = rsa.generate_private_key(
                    public_exponent=65537,
                    key_size=4096,
                    backend=default_backend())
            key_data = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=self.openssl_format,
                encryption_algorithm=self.no_encyption
            )
            with open(ca_key_path, 'wb') as f:
                f.write(key_data)
            return key

    def get_ca_cert(self, key):
        ca_cert_path = self.config.get('mtls', 'ca_cert')
        if not os.path.isabs(ca_cert_path):
            ca_cert_path = os.path.abspath(
                os.path.join(
                    os.path.dirname(__file__),
                    ca_cert_path
                )
            )

        # Grab the CA Certificate from filesystem if it exists and return
        if os.path.isfile(ca_cert_path):
            with open(ca_cert_path, 'rb') as cert_file:
                ca_cert = x509.load_pem_x509_certificate(
                    cert_file.read(),
                    default_backend()
                )
                return ca_cert

        key_id = x509.SubjectKeyIdentifier.from_public_key(
            key.public_key()
        )
        subject = issuer = x509.Name([
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                self.config.get('mtls', 'issuer')
            )
        ])
        now = datetime.datetime.utcnow()
        serial = x509.random_serial_number()
        ca_cert = x509.CertificateBuilder() \
            .subject_name(subject) \
            .issuer_name(issuer) \
            .public_key(key.public_key()) \
            .serial_number(serial) \
            .not_valid_before(now) \
            .not_valid_after(now + datetime.timedelta(days=365)) \
            .add_extension(key_id, critical=False) \
            .add_extension(
                x509.AuthorityKeyIdentifier(
                    key_id.digest,
                    [x509.DirectoryName(issuer)],
                    serial
                ),
                critical=False) \
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=0),
                critical=True
            ) \
            .add_extension(x509.KeyUsage(digital_signature=True,
                                         content_commitment=False,
                                         key_encipherment=False,
                                         data_encipherment=False,
                                         key_agreement=False,
                                         key_cert_sign=True,
                                         crl_sign=True,
                                         encipher_only=False,
                                         decipher_only=False),
                           critical=True) \
            .sign(key, hashes.SHA256(), default_backend())
        with open(ca_cert_path, 'wb') as f:
            f.write(
                ca_cert.public_bytes(serialization.Encoding.PEM)
            )
        return ca_cert

    def generate_cert(self, csr, lifetime):
        ca_pkey = self.get_ca_key()
        ca_cert = self.get_ca_cert(ca_pkey)
        now = datetime.datetime.utcnow()
        lifetime_delta = now + datetime.timedelta(hours=int(lifetime))
        alts = []
        for alt in self.config.get('mtls', 'alternate_name').split(','):
            alts.append(x509.DNSName(u'{}'.format(alt)))
        cert = x509.CertificateBuilder().subject_name(
            csr.subject
        ).issuer_name(
            ca_cert.subject
        ).public_key(
            csr.public_key()
        ).serial_number(
            uuid.uuid4().int
        ).not_valid_before(
           now
        ).not_valid_after(
            lifetime_delta
        )

        if len(alts) > 0:
            cert = cert.add_extension(
                x509.SubjectAlternativeName(alts), critical=False
            )

        cert = cert.sign(
            private_key=ca_pkey,
            algorithm=hashes.SHA256(),
            backend=default_backend()
        )
        return cert.public_bytes(serialization.Encoding.PEM)
