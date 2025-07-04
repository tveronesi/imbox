import imaplib
import io
import re
import email
import chardet
import base64
import quopri
import time
from datetime import datetime
from email.header import decode_header, Header
from imbox.utils import str_encode, str_decode

import logging

logger = logging.getLogger(__name__)


class Struct:
    def __init__(self, **entries):
        self.__dict__.update(entries)

    def keys(self):
        return self.__dict__.keys()

    def __repr__(self):
        return str(self.__dict__)


def decode_mail_header(value, default_charset='us-ascii'):
    """
    Decode a header value into a unicode string.
    """
    try:
        headers = decode_header(value)
    except email.errors.HeaderParseError:
        return str_decode(str_encode(value, default_charset, 'replace'), default_charset)
    else:
        for index, (text, charset) in enumerate(headers):
            try:
                logger.debug("Mail header no. {index}: {data} encoding {charset}".format(
                    index=index,
                    data=str_decode(text, charset or 'utf-8', 'replace'),
                    charset=charset))
                headers[index] = str_decode(text, charset or default_charset,
                                            'replace')
            except LookupError:
                # if the charset is unknown, force default
                headers[index] = str_decode(text, default_charset, 'replace')

        return ''.join(headers)


def get_mail_addresses(message, header_name):
    """
    Retrieve all email addresses from one message header.
    """
    headers = [h for h in message.get_all(header_name, [])]
    
    clean_headers = []
    # method message.get_all returns Header objects if it finds encoding errors
    # but next method email.utils.getaddresses() don't work with Header objects,
    # so we need to convert them to strings replacing bad chars with ???
    for h in headers:
        if isinstance(h, Header):
            h = h.__str__().encode('ascii','replace').decode()
        clean_headers.append(h)
    headers = clean_headers

    addresses = email.utils.getaddresses(headers)

    for index, (address_name, address_email) in enumerate(addresses):
        addresses[index] = {'name': decode_mail_header(address_name),
                            'email': address_email}
        logger.debug("{} Mail address in message: <{}> {}".format(
            header_name.upper(), address_name, address_email))
    return addresses


def decode_param(param):
    name, v = param.split('=', 1)
    values = v.split('\n')
    value_results = []
    for value in values:
        match = re.findall(r'=\?((?:\w|-)+)\?([QB])\?(.+?)\?=', value)
        if match:
            for encoding, type_, code in match:
                if type_ == 'Q':
                    value = quopri.decodestring(code)
                elif type_ == 'B':
                    value = code.encode()
                    missing_padding = len(value) % 4

                    if missing_padding:
                        value += b"=" * (4 - missing_padding)

                    value = base64.b64decode(value)

                value = str_encode(value, encoding)

                value_results.append(value)

    if value_results:
        v = ''.join(value_results)

    logger.debug("Decoded parameter {} - {}".format(name, v))
    return name, v


def parse_content_disposition(content_disposition):
    # Split content disposition on semicolon except when inside a string
    in_quote = False
    str_start = 0
    ret = []

    for i in range(len(content_disposition)):
        if content_disposition[i] == ';' and not in_quote:
            ret.append(content_disposition[str_start:i])
            str_start = i+1
        elif content_disposition[i] == '"' or content_disposition[i] == "'":
            in_quote = not in_quote

    if str_start < len(content_disposition):
        ret.append(content_disposition[str_start:])

    return ret



def parse_attachment(message_part):
    # Check again if this is a valid attachment
    content_disposition = message_part.get("Content-Disposition", None)
    if content_disposition is not None and not message_part.is_multipart():

        # if content_dispositon is type header converto to string
        if isinstance(content_disposition, Header):
            content_disposition = str(content_disposition)

        dispositions = [
            disposition.strip()
            for disposition in parse_content_disposition(content_disposition)
            if disposition.strip()
        ]

        if dispositions[0].lower() in ["attachment", "inline"]:
            file_data = message_part.get_payload(decode=True)

            attachment = {
                'content-type': message_part.get_content_type(),
                'size': len(file_data),
                'content': io.BytesIO(file_data),
                'content-id': message_part.get("Content-ID", None)
            }
            filename_parts = []
            for param in dispositions[1:]:
                if param:
                    name, value = decode_param(param)

                    # Check for split filename
                    s_name = name.rstrip('*').split("*")
                    if s_name[0] == 'filename':
                        try:
                            # If this is a split file name - use the number after the * as an index to insert this part
                            if len(s_name) > 1 and s_name[1] != '':
                                filename_parts.insert(int(s_name[1]),value[1:-1] if value.startswith('"') else value)
                            else:
                                filename_parts.insert(0,value[1:-1] if value.startswith('"') else value)
                        except Exception as err:
                            logger.debug('Parse attachment name error: %s', err)
                            filename_parts.insert(0, value)

                    if 'create-date' in name:
                        attachment['create-date'] = value

            attachment['filename'] = "".join(filename_parts)
            return attachment

    return None


def decode_content(message):
    content = message.get_payload(decode=True)
    charset = message.get_content_charset('utf-8')
    try:
        return content.decode(charset, 'ignore')
    except LookupError:
        encoding = chardet.detect(content).get('encoding')
        if encoding:
            return content.decode(encoding, 'ignore')
        return content
    except AttributeError:
        return content


def fetch_email_by_uid(uid, connection, parser_policy):
    message, data = connection.uid('fetch', uid, '(BODY.PEEK[] FLAGS)')
    logger.debug("Fetched message for UID {}".format(int(uid)))

    raw_headers = data[0][0] + data[1]
    raw_email = data[0][1]

    email_object = parse_email(raw_email, policy=parser_policy)
    flags = parse_flags(raw_headers.decode())
    email_object.__dict__['flags'] = flags

    return email_object


def parse_flags(headers):
    """Copied from https://github.com/girishramnani/gmail/blob/master/gmail/message.py"""
    if len(headers) == 0:
        return []
    headers = bytes(headers, "ascii")
    return list(imaplib.ParseFlags(headers))


def parse_email(raw_email, policy=None):
    if policy is not None:
        email_parse_kwargs = dict(policy=policy)
    else:
        email_parse_kwargs = {}

    # Should first get content charset then str_encode with charset.
    if isinstance(raw_email, bytes):
        email_message = email.message_from_bytes(
            raw_email, **email_parse_kwargs)
        charset = email_message.get_content_charset('utf-8')
        raw_email = str_encode(raw_email, charset, errors='ignore')
    else:
        try:
            email_message = email.message_from_string(
                raw_email, **email_parse_kwargs)
        except UnicodeEncodeError:
            email_message = email.message_from_string(
                raw_email.encode('utf-8'), **email_parse_kwargs)

    maintype = email_message.get_content_maintype()
    parsed_email = {'raw_email': raw_email}

    body = {
        "plain": [],
        "html": []
    }
    attachments = []

    if maintype in ('multipart', 'image'):
        logger.debug("Multipart message. Will process parts.")
        for part in email_message.walk():
            content_type = part.get_content_type()
            part_maintype = part.get_content_maintype()
            content_disposition = part.get('Content-Disposition', None)

            # if content_disposition is type Header then convert to string
            if isinstance(content_disposition, Header):
                content_disposition = str(content_disposition)

            if content_disposition or not part_maintype == "text":
                content = part.get_payload(decode=True)
            else:
                content = decode_content(part)

            is_inline = content_disposition is None \
                or content_disposition.startswith("inline")
            if content_type == "text/plain" and is_inline:
                body['plain'].append(content)
            elif content_type == "text/html" and is_inline:
                body['html'].append(content)
            elif content_disposition:
                attachment = parse_attachment(part)
                if attachment:
                    attachments.append(attachment)

    elif maintype == 'text':
        payload = decode_content(email_message)
        body['plain'].append(payload)

    elif maintype == 'application':
            if email_message.get_content_subtype() == 'pdf':
                attachment = parse_attachment(email_message)
                if attachment:
                    attachments.append(attachment)

    parsed_email['attachments'] = attachments

    parsed_email['body'] = body
    email_dict = dict(email_message.items())

    parsed_email['sent_from'] = get_mail_addresses(email_message, 'from')
    parsed_email['sent_to'] = get_mail_addresses(email_message, 'to')
    parsed_email['cc'] = get_mail_addresses(email_message, 'cc')
    parsed_email['bcc'] = get_mail_addresses(email_message, 'bcc')

    value_headers_keys = ['subject', 'date', 'message-id']
    key_value_header_keys = ['received-spf',
                             'mime-version',
                             'x-spam-status',
                             'x-spam-score',
                             'content-type']

    parsed_email['headers'] = []
    for key, value in email_dict.items():

        if key.lower() in value_headers_keys:
            valid_key_name = key.lower().replace('-', '_')
            parsed_email[valid_key_name] = decode_mail_header(value)

        if key.lower() in key_value_header_keys:
            parsed_email['headers'].append({'Name': key,
                                            'Value': value})

    if parsed_email.get('date'):
        parsed_email['parsed_date'] = email.utils.parsedate_to_datetime(parsed_email['date'])

    logger.info("Downloaded and parsed mail '{}' with {} attachments".format(
        parsed_email.get('subject'), len(parsed_email.get('attachments'))))
    return Struct(**parsed_email)
