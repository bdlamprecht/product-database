import logging

from django.shortcuts import render_to_response
from django.shortcuts import redirect
from django.shortcuts import resolve_url
from django.template import RequestContext
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.decorators import login_required
from django.utils.safestring import mark_safe
from djcelery.models import WorkerState

import app.productdb.tasks as tasks
from app.productdb import util as app_util
from app.productdb.models import ProductList
from app.productdb.models import Vendor
from app.productdb.models import Settings
from app.productdb.models import Product
from app.productdb.forms import CiscoApiSettingsForm
from app.productdb.forms import CommonSettingsForm
from app.productdb.extapi import ciscoapiconsole
from app.productdb.extapi.exception import InvalidClientCredentialsException
from app.productdb.extapi.exception import CiscoApiCallFailed
from app.productdb.extapi.exception import ConnectionFailedException
from app.productdb.crawler.cisco_eox_api_crawler import update_cisco_eox_database

logger = logging.getLogger(__name__)


def home(request):
    """view for the homepage of the Product DB

    :param request:
    :return:
    """
    return render_to_response("productdb/home.html", context={}, context_instance=RequestContext(request))


def about_view(request):
    """about view

    :param request:
    :return:
    """
    return render_to_response("productdb/about.html", context={}, context_instance=RequestContext(request))


def browse_product_list(request):
    """View to browse the product by product list

    :param request:
    :return:
    """
    context = {
        "product_lists": ProductList.objects.all()
    }
    selected_product_list = ""

    if request.method == "POST":
        selected_product_list = request.POST['product_list_selection']
    else:
        default_list_name = "Cisco Catalyst 2960X"
        for product_list in context['product_lists']:
            if product_list.product_list_name == default_list_name:
                selected_product_list = product_list.id
                break

    context['selected_product_list'] = selected_product_list
    return render_to_response("productdb/browse/product_lists.html",
                              context=context,
                              context_instance=RequestContext(request))


def browse_vendor_products(request):
    """View to browse the product by vendor

    :param request:
    :return:
    """
    context = {
        "vendors": Vendor.objects.all()
    }
    selected_vendor = ""

    if request.method == "POST":
        selected_vendor = request.POST['vendor_selection']
    else:
        default_vendor = "Cisco Systems"
        for vendor in context['vendors']:
            if vendor.name == default_vendor:
                selected_vendor = vendor.id
                break

    context['vendor_selection'] = selected_vendor

    return render_to_response("productdb/browse/vendor_products.html",
                              context=context,
                              context_instance=RequestContext(request))


def browse_product_lifecycle_information(request):
    """View to browse the lifecycle information for products by vendor

    :param request:
    :return:
    """
    context = {
        "vendors": Vendor.objects.all()
    }
    selected_vendor = ""

    if request.method == "POST":
        selected_vendor = request.POST['vendor_selection']

    context['vendor_selection'] = selected_vendor

    return render_to_response("productdb/lifecycle/lifecycle_information_by_vendor_products.html",
                              context=context,
                              context_instance=RequestContext(request))


def bulk_eol_check(request):
    """view that executes and handles the Bulk EoL check function

    :param request:
    :return:
    """
    context = {}

    if request.method == "POST":
        db_queries = request.POST['db_query'].splitlines()

        # clean POST db queries
        clean_db_queries = []
        for q in db_queries:
            clean_db_queries.append(q.strip())
        db_queries = filter(None, clean_db_queries)

        # detailed product results
        query_result = []
        # result statistics
        result_stats = dict()
        # queries, that are not found in the database or that are not affected by an EoL announcement
        skipped_queries = dict()

        for query in db_queries:
            q_result_counter = 0
            found_but_no_eol_announcement = False
            db_result = Product.objects.filter(product_id=query.strip())

            for element in db_result:
                q_result_counter += 1

                # check if the product is affected by an EoL announcement
                if element.eol_ext_announcement_date is None:
                    found_but_no_eol_announcement = True

                # don't add duplicates to query result, create statistical element
                if element.product_id not in result_stats.keys():
                    query_result.append(app_util.normalize_date(element))
                    result_stats[element.product_id] = dict()
                    result_stats[element.product_id]['count'] = 1
                    result_stats[element.product_id]['product'] = element
                    if element.eol_ext_announcement_date:
                        result_stats[element.product_id]['state'] = "EoS/EoL"
                    else:
                        result_stats[element.product_id]['state'] = "Not EoL"

                # increment statistics
                else:
                    result_stats[element.product_id]['count'] += 1

            if (q_result_counter == 0) or found_but_no_eol_announcement:
                if found_but_no_eol_announcement:
                    q_res_str = "no EoL announcement found"
                else:
                    # add queries without result to the stats and the counter
                    q_res_str = "Not found in database"
                    if query not in result_stats.keys():
                        print("Q " + query)
                        result_stats[query] = dict()
                        result_stats[query]['state'] = "Not found"
                        result_stats[query]['product'] = dict()
                        result_stats[query]['product']['product_id'] = query
                        result_stats[query]['count'] = 1
                    else:
                        result_stats[query]['count'] += 1

                # ignore duplicates
                if query not in skipped_queries.keys():
                    skipped_queries[query] = {
                        "query": query.strip(),
                        "result": q_res_str
                    }

        context['query_result'] = query_result
        context['result_stats'] = result_stats
        context['skipped_queries'] = skipped_queries

        # simply display an error message if no result is found
        if len(query_result) == 0:
            context['query_no_result'] = True

    return render_to_response("productdb/lifecycle/bulk_eol_check.html",
                              context=context,
                              context_instance=RequestContext(request))


@login_required()
@permission_required('is_superuser')
def settings_view(request):
    """View for common product DB settings

    :param request:
    :return:
    """
    settings, created = Settings.objects.get_or_create(id=0)

    if request.method == 'POST':
        # create a form instance and populate it with data from the request:
        form = CommonSettingsForm(request.POST)
        if form.is_valid():
            # process the data in form.cleaned_data as required
            settings.cisco_api_enabled = form.cleaned_data['cisco_api_enabled']
            if not settings.cisco_api_enabled:
                # reset values from API configuration
                base_api = ciscoapiconsole.BaseCiscoApiConsole()
                base_api.client_id = "PlsChgMe"
                base_api.client_secret = "PlsChgMe"
                base_api.save_client_credentials()

                settings.cisco_eox_api_auto_sync_enabled = False
                settings.eox_api_blacklist = ""
                settings.cisco_eox_api_auto_sync_queries = ""
                settings.cisco_api_credentials_last_message = "not tested"
                settings.cisco_api_credentials_successful_tested = False

            settings.save()

            return redirect(resolve_url("productdb:settings"))

    else:
        form = CommonSettingsForm()
        form.fields['cisco_api_enabled'].initial = settings.cisco_api_enabled

    context = {
        "form": form,
        "settings": settings
    }

    return render_to_response("productdb/settings/settings.html",
                              context=context,
                              context_instance=RequestContext(request))


@login_required()
@permission_required('is_superuser')
def cisco_api_settings(request):
    """View for the settings of the Cisco API console

    :param request:
    :return: :raise:
    """
    settings, created = Settings.objects.get_or_create(id=0)

    if request.method == "POST":
        # create a form instance and populate it with data from the request:
        form = CiscoApiSettingsForm(request.POST)
        if form.is_valid():
            # process the data in form.cleaned_data as required
            settings.cisco_eox_api_auto_sync_auto_create_elements = \
                form.cleaned_data['eox_auto_sync_auto_create_elements']
            settings.cisco_eox_api_auto_sync_enabled = form.cleaned_data['eox_api_auto_sync_enabled']
            settings.cisco_eox_api_auto_sync_queries = form.cleaned_data['eox_api_queries']
            settings.eox_api_blacklist = form.cleaned_data['eox_api_blacklist']

            base_api = ciscoapiconsole.CiscoHelloApi()
            base_api.load_client_credentials()

            old_credentials = str(base_api.client_id) + str(base_api.client_secret)
            new_credentials = form.cleaned_data['cisco_api_client_id'] + form.cleaned_data['cisco_api_client_secret']

            # Test of the credentials is only required if these are changed
            if not new_credentials == old_credentials:
                base_api.client_id = form.cleaned_data['cisco_api_client_id']
                base_api.client_secret = form.cleaned_data['cisco_api_client_secret']
                base_api.save_client_credentials()

                # test credentials (if not in demo mode)
                if settings.demo_mode:
                    logger.warn("skipped verification of the Hello API call to test the credentials, "
                                "DEMO MODE enabled")
                    settings.cisco_api_credentials_successful_tested = True
                    settings.cisco_api_credentials_last_message = "Demo Mode"
                else:
                    try:
                        base_api.hello_api_call()
                        settings.cisco_api_credentials_successful_tested = True
                        settings.cisco_api_credentials_last_message = "successful connected"

                    except InvalidClientCredentialsException as ex:
                        settings.cisco_api_credentials_successful_tested = False
                        logger.warn("verification of client credentials failed", exc_info=True)
                        settings.cisco_api_credentials_last_message = str(ex)

            settings.save()
            return redirect(resolve_url("productdb:cisco_api_settings"))

    else:
        form = CiscoApiSettingsForm()
        form.fields['eox_auto_sync_auto_create_elements'].initial = settings.cisco_eox_api_auto_sync_auto_create_elements
        form.fields['eox_api_auto_sync_enabled'].initial = settings.cisco_eox_api_auto_sync_enabled
        form.fields['eox_api_queries'].initial = settings.cisco_eox_api_auto_sync_queries
        form.fields['eox_api_blacklist'].initial = settings.eox_api_blacklist

        if settings.cisco_api_enabled:
            try:
                base_api = ciscoapiconsole.CiscoHelloApi()
                base_api.load_client_credentials()
                # load the client credentials if exist
                cisco_api_credentials = base_api.get_client_credentials()

                form.fields['cisco_api_client_id'].initial = cisco_api_credentials['client_id']
                form.fields['cisco_api_client_secret'].initial = cisco_api_credentials['client_secret']

            except Exception:
                logger.fatal("unexpected exception occurred", exc_info=True)
                raise
        else:
            form.cisco_api_client_id = False
            form.fields['cisco_api_client_secret'].required = False

    context = {
        "settings_form": form,
        "settings": settings
    }

    return render_to_response("productdb/settings/cisco_api_settings.html",
                              context=context,
                              context_instance=RequestContext(request))


@login_required()
@permission_required('is_superuser')
def crawler_overview(request):
    """Overview of the tasks

    :param request:
    :return:
    """
    settings, created = Settings.objects.get_or_create(id=0)

    context = {
        "settings": settings
    }

    # determine worker status
    ws = WorkerState.objects.all()
    if ws.count() == 0:
        worker_status = """
        <div class="alert alert-danger" role="alert">
            <span class="glyphicon glyphicon-exclamation-sign" aria-hidden="true"></span>
            <span class="sr-only">Error:</span>
            No worker found, periodic and scheduled tasks will not run
        </div>"""
    else:
        alive_worker = False
        for w in ws:
            if w.is_alive():
                alive_worker = True
                break
        if alive_worker:
            worker_status = """
            <div class="alert alert-success" role="alert">
                <span class="glyphicon glyphicon-exclamation-sign" aria-hidden="true"></span>
                <span class="sr-only">Error:</span>
                Online Worker found, task backend running.
            </div>"""

        else:
            worker_status = """
            <div class="alert alert-warning" role="alert">
                <span class="glyphicon glyphicon-exclamation-sign" aria-hidden="true"></span>
                <span class="sr-only">Error:</span>
                Only offline Worker found, task backend not running. Please verify the state in the
                <a href="/admin">Django Admin</a> frontend.
            </div>"""

    context['worker_status'] = mark_safe(worker_status)

    return render_to_response("productdb/settings/crawler_overview.html",
                              context=context,
                              context_instance=RequestContext(request))


@login_required()
@permission_required('is_superuser')
def test_tools(request):
    """test tools for the application (mainly about the crawler functions)

    :param request:
    :return:
    """
    settings, created = Settings.objects.get_or_create(id=0)

    context = {
        "settings": settings
    }

    if request.method == "POST":
        # create a form instance and populate it with data from the request:
        if "sync_cisco_eox_states_now" in request.POST.keys():
            if "sync_cisco_eox_states_query" in request.POST.keys():
                query = request.POST['sync_cisco_eox_states_query']

                if query != "":
                    if len(query.split(" ")) == 1:
                        context['query_executed'] = query
                        try:
                            eox_api_update_records = update_cisco_eox_database(api_queries=query)

                        except ConnectionFailedException as ex:
                            eox_api_update_records = ["Cannot contact Cisco API, error message:\n%s" % ex]

                        except CiscoApiCallFailed as ex:
                            eox_api_update_records = [ex]

                        except Exception as ex:
                            logger.debug("execution failed due to unexpected exception", exc_info=True)
                            eox_api_update_records = ["execution failed: %s" % ex]

                        context['eox_api_update_records'] = eox_api_update_records

                    else:
                        context['eox_api_update_records'] = ["Invalid query '%s': not executed" %
                                                             request.POST['sync_cisco_eox_states_query']]
                else:
                    context['eox_api_update_records'] = ["Please specify a valid query"]

    # determine worker status
    ws = WorkerState.objects.all()
    if ws.count() == 0:
        worker_status = """
        <div class="alert alert-danger" role="alert">
            <span class="glyphicon glyphicon-exclamation-sign" aria-hidden="true"></span>
            <span class="sr-only">Error:</span>
            No worker found, periodic and scheduled tasks will not run
        </div>"""
    else:
        alive_worker = False
        for w in ws:
            if w.is_alive():
                alive_worker = True
                break
        if alive_worker:
            worker_status = """
            <div class="alert alert-success" role="alert">
                <span class="glyphicon glyphicon-exclamation-sign" aria-hidden="true"></span>
                <span class="sr-only">Error:</span>
                Online Worker found, task backend running.
            </div>"""

        else:
            worker_status = """
            <div class="alert alert-warning" role="alert">
                <span class="glyphicon glyphicon-exclamation-sign" aria-hidden="true"></span>
                <span class="sr-only">Error:</span>
                Only offline Worker found, task backend not running. Please verify the state in the
                <a href="/admin">Django Admin</a> frontend.
            </div>"""

    context['worker_status'] = mark_safe(worker_status)

    return render_to_response("productdb/settings/task_testing_tools.html",
                              context=context,
                              context_instance=RequestContext(request))


@login_required()
@permission_required('is_superuser')
def schedule_cisco_eox_api_sync_now(request):
    """View which manually schedules an Cisco EoX synchronization and redirects to the given URL
    or the main settings page.

    :param request:
    :return:
    """
    s = Settings.objects.get(id=0)

    task = tasks.execute_task_to_synchronize_cisco_eox_states.delay()
    s.eox_api_sync_task_id = task.id
    s.save()

    return redirect(request.GET.get('redirect_url', "/productdb/settings/"))