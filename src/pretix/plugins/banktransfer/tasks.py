import json
import logging
import re
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils.translation import ugettext_noop

from pretix.base.i18n import language
from pretix.base.models import Event, Order, Quota
from pretix.base.services.mail import SendMailException
from pretix.base.services.orders import mark_order_paid

from .models import BankImportJob, BankTransaction

logger = logging.getLogger(__name__)


def _handle_transaction(event: Event, trans: BankTransaction, code: str):
    try:
        trans.order = event.orders.get(code=code)
    except Order.DoesNotExist:
        normalized_code = Order.normalize_code(code)
        try:
            trans.order = event.orders.get(code=normalized_code)
        except Order.DoesNotExist:
            trans.state = BankTransaction.STATE_NOMATCH
            trans.save()
            return

    if trans.order.status == Order.STATUS_PAID:
        trans.state = BankTransaction.STATE_DUPLICATE
    elif trans.order.status == Order.STATUS_REFUNDED:
        trans.state = BankTransaction.STATE_ERROR
        trans.message = ugettext_noop('The order has already been refunded.')
    elif trans.order.status == Order.STATUS_CANCELLED:
        trans.state = BankTransaction.STATE_ERROR
        trans.message = ugettext_noop('The order has already been cancelled.')
    elif trans.amount != trans.order.total:
        trans.state = BankTransaction.STATE_INVALID
        trans.message = ugettext_noop('The transaction amount is incorrect.')
    else:
        try:
            mark_order_paid(trans.order, provider='banktransfer', info=json.dumps({
                'reference': trans.reference,
                'date': trans.date,
                'payer': trans.payer,
                'trans_id': trans.pk
            }))
        except Quota.QuotaExceededException as e:
            trans.state = BankTransaction.STATE_ERROR
            trans.message = str(e)
        except SendMailException:
            trans.state = BankTransaction.STATE_ERROR
            trans.message = ugettext_noop('Problem sending email.')
        else:
            trans.state = BankTransaction.STATE_VALID
    trans.save()


def _get_unknown_transactions(event: Event, job: BankImportJob, data: list):
    amount_pattern = re.compile("[^0-9.-]")
    known_checksums = set(t['checksum'] for t in BankTransaction.objects.filter(event=event).values('checksum'))

    transactions = []
    for row in data:
        amount = amount_pattern.sub("", row['amount'].replace(",", "."))
        try:
            amount = Decimal(amount)
        except:
            logger.exception('Could not parse amount of transaction: {}'.format(amount))
            amount = Decimal("0.00")

        trans = BankTransaction(event=event, import_job=job,
                                payer=row['payer'],
                                reference=row['reference'],
                                amount=amount,
                                date=row['date'])
        trans.checksum = trans.calculate_checksum()
        if trans.checksum not in known_checksums:
            trans.state = BankTransaction.STATE_UNCHECKED
            trans.save()
            transactions.append(trans)
            known_checksums.add(trans.checksum)

    return transactions


def process_banktransfers(event: int, job: int, data: list) -> None:
    with language("en"):  # We'll translate error messages at display time
        event = Event.objects.get(pk=event)
        job = BankImportJob.objects.get(pk=job)
        job.state = BankImportJob.STATE_RUNNING
        job.save()

        try:
            transactions = _get_unknown_transactions(event, job, data)

            code_len = settings.ENTROPY['order_code']
            pattern = re.compile(event.slug.upper() + "[ \-_]*([A-Z0-9]{%s})" % code_len)

            for trans in transactions:
                match = pattern.search(trans.reference.upper())

                if match:
                    code = match.group(1)
                    with transaction.atomic():
                        _handle_transaction(event, trans, code)
                else:
                    trans.state = BankTransaction.STATE_NOMATCH
                    trans.save()
        except Exception as e:
            job.state = BankImportJob.STATE_ERROR
            job.save()
            raise e
        else:
            job.state = BankImportJob.STATE_COMPLETED
            job.save()


if settings.HAS_CELERY:
    from pretix.celery import app

    process_task = app.task(process_banktransfers)

    def process_banktransfers(*args, **kwargs):
        process_task.apply_async(args=args, kwargs=kwargs)
