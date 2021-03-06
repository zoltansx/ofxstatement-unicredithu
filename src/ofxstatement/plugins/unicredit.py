""" Unicredit plugin implements a CAMT.053 reader, specialized to parse
the schema used in Unicredit Bank's exports"""

import xml.etree.ElementTree as ET
import datetime
import re

from ofxstatement.plugin import Plugin
from ofxstatement.parser import StatementParser
from ofxstatement.statement import Statement, StatementLine


CD_CREDIT = 'CRDT'
CD_DEBIT = 'DBIT'

def normalize_account_id(account_id):
    """ Strips dashes from the account_id and the trailing digit as it's not
    part of the account id, but a CRC and finance apps stripping it either

        IBAN Fields: HUkk bbbs sssx cccc cccc cccc cccx
        b = National bank code
        s = Branch code
        c = Account number
        x = National check digit"""

    if account_id is None:
        return account_id

    return account_id.replace('-', '')[:23]


class UnicreditPlugin(Plugin):
    """Unicredit (XML)
    """

    def get_parser(self, filename):
        """ Initializes and returns the parser object"""
        parser = UnicreditParser(filename)
        # We allow dashes as separator in the config

        parser.account_id = normalize_account_id(self.settings.get('account'))

        return parser


class UnicreditParser(StatementParser):
    """Unicredit CAMT.053 parser"""
    def __init__(self, filename):
        self.filename = filename
        # Tells which statement we need.
        # Can be None if there's only one Statement in the CAMT.053 file
        self.account_id = None
        self.statement = None

    def _pick_matching_statement(self, stmts):
        """Groups statements by account ids and returns the statements the belong
        to self.account_id"""
        if not stmts:
            raise Exception("No statement data in the file")

        stmt_by_acct = {
            normalize_account_id(
                _find(stmt, 'Acct/Id/Othr/Id').text): stmt for stmt in stmts}

        if self.account_id is None:
            if len(stmts) == 1:
                return stmts[0]

            raise Exception(
                "You have more than one accounts, please configure them "
                "with ofxstatement edit-config: %s" % ", ".
                join(stmt_by_acct.keys()))

        if self.account_id in stmt_by_acct.keys():
            return stmt_by_acct[self.account_id]

        raise Exception(
            "The account you specified ('%s') is not among the "
            "ones in the file, please configure them "
            "with ofxstatement edit-config: %s" %
            (self.account_id, ", ".join(stmt_by_acct.keys())))

    def split_records(self):
        """Parses the XML/CAMT file, sets start/end balance belonging to self.account_id, and returns statements
        of that account.
        """
        tree = ET.parse(self.filename)

        stmts = _findall(tree, 'BkToCstmrStmt/Stmt')

        # Pick statement matching self.account_id
        stmt = self._pick_matching_statement(stmts)

        # Set core Statement data
        bank_id = _get_text(_find(stmt, 'Acct/Svcr/FinInstnId/BIC'),
                            'UNICREDIT')
        iban = _get_text(_find(stmt, 'Acct/Id/Othr/Id'))
        ccy = _get_text(_find(stmt, 'Acct/Ccy'), 'HUF')

        account_id = normalize_account_id(iban)
        self.statement = Statement(bank_id, account_id, ccy)

        # Set balance data
        (bal_amts, bal_dates) = get_balance_data(_findall(stmt, 'Bal'))

        # Should be done afterwards
        self.statement.start_balance = bal_amts['OPBD']
        self.statement.start_date = bal_dates['OPBD']

        self.statement.end_balance = bal_amts.get('CLBD')
        self.statement.end_date = bal_dates.get('CLBD')

        return _findall(stmt, 'Ntry')

    def parse_record(self, ntry):
        """Parses an Ntry and returns data in a StatementLine object"""

        # def __init__(self, id=None, date=None, memo=None, amount=None):
        sline = StatementLine()

        # Let's have a default for line parser
        sline.peer_acct = None

        # TODO set trntype - see statement.py in ofxstatement
        # It's now defaulted to 'CHECkK'
        # 11.4.4.3 Transaction Types Used in <TRNTYPE>
        # Type | Description
        # CREDIT | Generic credit
        # DEBIT | Generic debit
        # INT | Interest earned or paid
        #   Note: Depends on signage of amount DIV Dividend
        # FEE | FI fee
        # SRVCHG | Service charge DEP Deposit
        # ATM | ATM debit or credit
        # POS Point of sale debit or credit
        #  Note: Depends on signage of amount
        # XFER | Transfer
        # CHECK | Check
        # PAYMENT | Electronic payment
        # CASH | Cash withdrawal
        # DIRECTDEP | Direct deposit
        # DIRECTDEBIT | Merchant initiated debit
        # REPEATPMT | Repeating payment/standing order
        # HOLD | Only valid in <STMTTRNP>; indicates the amount is under a hold
        #  Note: Depends on signage of amount and account type
        # OTHER | Other

        crdeb = _find(ntry, 'CdtDbtInd').text

        amtnode = _find(ntry, 'Amt')
        amt = _parse_amount(amtnode)

        txid = _find(ntry, 'NtryDtls/TxDtls/Refs/TxId')
        if txid.text:
            sline.txid = txid.text
            # if sline.txid == '00000000002038112324':
            #     import pdb; pdb.set_trace()

        if crdeb == CD_DEBIT:
            amt = -amt
            payee = _find(ntry, 'NtryDtls/TxDtls/RltdPties/Cdtr/Nm')
            if _get_text(_find(ntry, 'NtryDtls/TxDtls/RmtInf/Ustrd'), '').startswith('ATM kifizetés'):
                sline.trntype = 'ATM'
            else:
                sline.trntype = 'DEBIT'
                sline.peer_acct = _get_text(_find(ntry, 'NtryDtls/TxDtls/RltdPties/CdtrAcct/Id/IBAN'))
        else:
            payee = _find(ntry, 'NtryDtls/TxDtls/RltdPties/Dbtr/Nm')
            sline.trntype = 'CREDIT'
            sline.peer_acct = _get_text(_find(ntry, 'NtryDtls/TxDtls/RltdPties/DbtrAcct/Id/IBAN'))

        if payee is not None:
            payee = payee.text

        sline.payee = payee
        sline.amount = amt

        date = _find(ntry, 'ValDt')
        sline.date = _parse_date(date)

        bookdt = _find(ntry, 'BookgDt')
        sline.date_user = _parse_date(bookdt)

        svcref = _find(ntry, 'AcctSvcrRef')
        sline.refnum = getattr(svcref, 'text', None)
        # To have FITID
        sline.id = sline.refnum

        rmtinf = _find(ntry, 'NtryDtls/TxDtls/RmtInf/Ustrd')
        sline.memo = rmtinf.text if (rmtinf is not None and rmtinf.text) else ''

        addtlinf = _find(ntry, 'NtryDtls/TxDtls/AddtlTxInf').text

        # Card transaction
        if addtlinf.startswith('+CMS CLT') and not sline.payee:
            match_obj = re.match(
                r'V.s.rl.s\(\d{4}\.\d{2}\.\d{2}\)  +'
                r'Card:\d{16}  +(.*) [0-9.]+,00 HUF$', sline.memo)
            if match_obj is not None:
                sline.payee = match_obj.group(1)

        sline.memo += ' ' + addtlinf

        return sline


def _get_text(elem, default=None):
    return elem.text if elem is not None else default


def get_balance_data(bals):
    """Retrieves opening, closing balance data for the statement"""
    bal_amts = {}
    bal_dates = {}

    for bal in bals:
        clop = _find(bal, 'Tp/CdOrPrtry/Cd')
        amt = _find(bal, 'Amt')
        date = _find(bal, 'Dt')

        # Amount currency should match with statement currency
        bal_amts[clop.text] = _parse_amount(amt)
        bal_dates[clop.text] = _parse_date(date)

    return (bal_amts, bal_dates)


def _parse_date(dtnode):
    """Parses a ValDt or BookgDt node and returns a datetime object
       holding the data"""
    if dtnode is None:
        return None

    date = _find(dtnode, 'Dt')
    dttm = _find(dtnode, 'DtTm')

    if date is not None:
        return datetime.datetime.strptime(date.text, "%Y-%m-%d")

    assert dttm is not None
    return datetime.datetime.strptime(dttm.text, "%Y-%m-%dT%H:%M:%S")


def _parse_amount(amtnode):
    return float(amtnode.text)


CAMT_PREFIX = 'camt.053'
CAMT_NS = {CAMT_PREFIX: 'urn:iso:std:iso:20022:tech:xsd:camt.053.001.02'}


def _toxpath(spath):
    tags = spath.split('/')
    path = ['%s:%s' % (CAMT_PREFIX, t) for t in tags]
    xpath = './%s' % '/'.join(path)
    return xpath


def _find(tree, spath):
    return tree.find(_toxpath(spath), CAMT_NS)


def _findall(tree, spath):
    return tree.findall(_toxpath(spath), CAMT_NS)
