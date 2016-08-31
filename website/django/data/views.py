import os
import re
import tempfile
import json
from operator import __or__

from django.db import connection
from django.db.models import Q
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.views.decorators.gzip import gzip_page

from .models import Variant
from django.views.decorators.http import require_http_methods

# GA4GH related imports
from ga4gh import variant_service_pb2 as variant_service
from ga4gh import variants_pb2 as variants
from ga4gh import metadata_service_pb2 as metadata_service
from ga4gh import metadata_pb2 as metadata
import google.protobuf.json_format as json_format

#########################################################

@gzip_page
def index(request):
    order_by = request.GET.get('order_by')
    direction = request.GET.get('direction')
    page_size = int(request.GET.get('page_size', '0'))
    page_num = int(request.GET.get('page_num', '0'))
    search_term = request.GET.get('search_term')
    format = request.GET.get('format')
    include = request.GET.getlist('include')
    exclude = request.GET.getlist('exclude')
    filters = request.GET.getlist('filter')
    filter_values = request.GET.getlist('filterValue')
    column = request.GET.getlist('column')

    query = Variant.objects

    if format == 'csv':
        quotes = '\''
    else:
        quotes = ''

    if include or exclude:
        query = apply_sources(query, include, exclude)

    if filters:
        query = apply_filters(query, filter_values, filters, quotes=quotes)

    if search_term:
        query = apply_search(query, search_term, quotes=quotes)

    if order_by:
        query = apply_order(query, order_by, direction)

    if format == 'csv':

        cursor = connection.cursor()
        with tempfile.NamedTemporaryFile() as f:
            os.chmod(f.name, 0606)
            cursor.execute("COPY ({}) TO '{}' WITH DELIMITER ',' CSV HEADER".format(query.query, f.name))

            response = HttpResponse(f.read(), content_type='text/csv')
            response['Content-Disposition'] = 'attachment;filename="variants.csv"'
            return response

    elif format == 'json':

        count = query.count()

        if search_term:
            # Number of synonym matches = total matches minus matches on "normal" columns
            synonyms = count - apply_search(query, search_term, search_column='fts_standard').count()
        else:
            synonyms = 0

        query = select_page(query, page_size, page_num)

        # call list() now to evaluate the query
        response = JsonResponse({'count': count, 'synonyms': synonyms, 'data': list(query.values(*column))})
        response['Access-Control-Allow-Origin'] = '*'
        return response


def apply_sources(query, include, exclude):
    # if there are multiple sources given then OR them:
    # the row must match in at least one column
    include_list = (Q(**{column: True}) for column in include)
    exclude_dict = {exclusion: False for exclusion in exclude}

    return query.filter(reduce(__or__, include_list)).filter(**exclude_dict)


def apply_filters(query, filterValues, filters, quotes=''):
    # if there are multiple filters the row must match all the filters
    for column, value in zip(filters, filterValues):
        if column == 'id':
            query = query.filter(**{column: value})
        else:
            query = query.extra(
                where=["\"{0}\" LIKE %s".format(column)],
                params=["{0}{1}%{0}".format(quotes, value)]
            )
    return query


def apply_search(query, search_term, search_column='fts_document', quotes=''):
    # search using the tsvector column which represents our document made of all the columns
    where_clause = "variant.{} @@ to_tsquery('simple', %s)".format(search_column)
    parameter = quotes + sanitise_term(search_term) + quotes
    return query.extra(
        where=[where_clause],
        params=[parameter]
    )


def apply_order(query, order_by, direction):
    # special case for HGVS columns
    if order_by in ('HGVS_cDNA', 'HGVS_Protein'):
        order_by = 'Genomic_Coordinate_hg38'
    if direction == 'descending':
        order_by = '-' + order_by
    return query.order_by(order_by, 'Pathogenicity_default')


def select_page(query, page_size, page_num):
    if page_size:
        start = page_size * page_num
        end = start + page_size
        return query[start:end]
    return query


def autocomplete(request):
    term = request.GET.get('term')
    limit = int(request.GET.get('limit', 10))

    cursor = connection.cursor()

    cursor.execute(
        """SELECT word FROM words
        WHERE word LIKE %s
        AND char_length(word) >= 3
        ORDER BY word""",
        ["%s%%" % term])

    rows = cursor.fetchall()

    response = JsonResponse({'suggestions': rows[:limit]})
    response['Access-Control-Allow-Origin'] = '*'
    return response


def sanitise_term(term):
    # Escape all non alphanumeric characters
    term = re.escape(term)
    # Enable prefix search
    term += ":*"
    return term

@require_http_methods(["POST"])
def search_variants(request):
    """Handles requests to the /variants/search method"""
    conditional = validate_search_variants_request(request)
    if conditional :
        return conditional
    else:
        # TODO use ga4gh protocol from json
        # json_format.Parse(json_string, variant_service.SearchVariantsRequest())
        req_dict = json.loads(request.body)
        variant_set_id = req_dict.get('variantSetId')
        reference_name = req_dict.get('referenceName')
        start = req_dict.get('start')
        end = req_dict.get('end')
        page_size = req_dict.get('pageSize', 0)
        page_token = req_dict.get('pageToken', '0')
    if not page_size or page_size == 0:
        page_size = DEFAULT_PAGE_SIZE
    if not page_token:
        page_token = '0'

    response = variant_service.SearchVariantsResponse()
    variants = Variant.objects
    reference_genome = variant_set_id.split('-')[1]
    variants = range_filter(reference_genome, variants, reference_name, start, end)
    variants = ga4gh_brca_page(variants, int(page_size), int(page_token))

    ga_variants = [brca_to_ga4gh(i, reference_genome) for i in variants.values()]
    if len(ga_variants) > page_size:
        ga_variants.pop()
        page_token = str(1 + int(page_token))
        response.next_page_token = page_token

    response.variants.extend(ga_variants)
    resp = json_format._MessageToJsonObject(response, False)
    return JsonResponse(resp)


def range_filter(reference_genome, variants, reference_name, start=None, end=None):
    """Filters variants by range depending on the reference_genome"""
    # TODO make sure start and end are set before this
    if 'chr' not in reference_name:
        reference_name = 'chr' + reference_name
    variants = variants.filter(Reference_Name=reference_name)
    if reference_genome == 'hg36':
        variants = variants.order_by('Hg36_Start')
        variants = variants.filter(Hg36_Start__lt=end, Hg36_End__gt=start)
    elif reference_genome == 'hg37':
        variants = variants.order_by('Hg37_Start')
        variants = variants.filter(Hg37_Start__lt=end, Hg37_End__gt=start)
    elif reference_genome == 'hg38':
        variants = variants.order_by('Hg38_Start')
        variants = variants.filter(Hg38_Start__lt=end, Hg38_End__gt=start)
    return variants

def ga4gh_brca_page(query, page_size, page_token):
    """Filters django queries by page for GA4GH requests"""
    start = page_size * page_token
    end = start + page_size + 1
    return query[start:end]

def get_offset(start, end, variant_length=None):
    """Auxiliary function to obtain offset for database query"""
    # FIXME currently unused
    if variant_length:
        start -= 1
        end = start + len(variant_length)
    else:
        start += 1
        end += 1
    return start, end

def brca_to_ga4gh(brca_variant, reference_genome):
    """Function that translates elements in BRCA-database to GA4GH format."""
    variant = variants.Variant()
    bases = brca_variant['Genomic_Coordinate_' + reference_genome].split(':')[2]
    variant.reference_bases, alternbases = bases.split('>')
    for i in range(len(alternbases)):
        variant.alternate_bases.append(alternbases[i])
    # TODO set this based on something...
    variant.created = 0
    variant.updated = 0
    variant.reference_name = brca_variant['Reference_Name']
    if reference_genome == 'hg36':
        variant.start = brca_variant['Hg36_Start']
        variant.end = brca_variant['Hg36_End']
    elif reference_genome == 'hg37':
        variant.start = brca_variant['Hg37_Start']
        variant.end = brca_variant['Hg37_End']
    elif reference_genome == 'hg38':
        variant.start = brca_variant['Hg38_Start']
        variant.end = brca_variant['Hg38_End']
    variant.id = '{}-{}'.format(reference_genome, str(brca_variant['id']))
    variant.variant_set_id = '{}-{}'.format(DATASET_ID, reference_genome)
    for name in str(brca_variant['Synonyms']).split(','):
        variant.names.append(name)
    for key in brca_variant:
        if brca_variant[key] != '-' and brca_variant[key] != '':
            variant.info[str(key)].append(brca_variant[key])
    return variant


def validate_search_variants_request(request):
    """Auxiliary function which validates search variants requests"""
    if not request.body:
        return HttpResponseBadRequest(
            json.dumps(ErrorMessages['emptyBody']),
            content_type='application/json')
    else:
        request_dict = json.loads(request.body)
        if not request_dict.get('variantSetId'):
            return HttpResponseBadRequest(
                json.dumps(ErrorMessages['variantSetId']),
                content_type='application/json')
        elif not request_dict.get('referenceName'):
            return HttpResponseBadRequest(
                json.dumps(ErrorMessages['referenceName']),
                content_type='application/json')
        elif not request_dict.get('start'):
            return HttpResponseBadRequest(
                json.dumps(ErrorMessages['start']),
                content_type='application/json')
        elif not request_dict.get('end') :
            return HttpResponseBadRequest(
                json.dumps(ErrorMessages['end']),
                content_type='application/json')
        else:
            # Make sure the variant set ID is well formed
            ids = request_dict.get('variantSetId').split('-')
            reference_name = request_dict.get('referenceName')
            if len(ids) < 2:
                return HttpResponse(
                    json.dumps(ErrorMessages['variantSetId']),
                               content_type='application/json',
                               status=404)
            reference_genome = ids[1]
            if reference_genome not in SET_IDS:
                return HttpResponse(
                    json.dumps(ErrorMessages['variantSetId']),
                    content_type='application/json',
                    status=404)
            if reference_name not in REFERENCE_NAMES:
                return HttpResponse(
                    json.dumps(ErrorMessages['referenceName']),
                    content_type='application/json',
                    status=404)
            return None

def validate_search_variant_sets_request(request):
    """Auxiliary function which validates search variant sets requests"""
    if not request.body:
        return HttpResponseBadRequest(
            json.dumps(ErrorMessages['emptyBody']),
            content_type='application/json')
    else:
        request_dict = json.loads(request.body)
        if not request_dict.get('datasetId'):
            return HttpResponseBadRequest(
                json.dumps(ErrorMessages['datasetId']),
                content_type='application/json')
        else:
            return None


@require_http_methods(['GET'])
def get_variant(request, variant_id):
    """Handles requests to the /variants/<variant id> endpoint"""
    if not variant_id:
        return HttpResponseBadRequest(
            json.dumps(ErrorMessages['variantId']),
            content_type='application/json')
    else:
        set_id, v_id = variant_id.split('-')
        if set_id in SET_IDS:
            variants = Variant.objects.values()
            # TODO fail safely to 404 if none found
            variant = variants.get(id=int(v_id))
            ga_variant = brca_to_ga4gh(variant, set_id)
            response = json_format._MessageToJsonObject(ga_variant, True)
            return JsonResponse(response)
        else:
            # TODO change to not found message
            return HttpResponse(
                json.dumps(ErrorMessages['datasetId']),
                content_type='application/json',
                status=404)

@require_http_methods(['POST'])
def search_variant_sets(request):
    """Handles requests at the /variantsets/search endpoint"""
    invalid_request = validate_search_variant_sets_request(request)
    if invalid_request:
        return invalid_request
    else:
        req_dict = json.loads(request.body)
        dataset_id = req_dict.get('datasetId')
        # TODO page size is unused, add paging
        page_size = req_dict.get('pageSize', DEFAULT_PAGE_SIZE)
        page_token = req_dict.get('pageToken', '0')
        if dataset_id != DATASET_ID:
            # TODO instead of bad request return empty list
            return HttpResponseBadRequest(
                json.dumps(ErrorMessages['datasetId']),
                content_type='application/json')
    if page_token is None:
         page_token = '0'

    response = variant_service.SearchVariantSetsResponse()
    response.next_page_token = page_token
    # TODO generalize by associating a function with each variant set ID
    for set_id in SET_IDS:
        variant_set = variants.VariantSet()
        variant_set.id = '{}-{}'.format(DATASET_ID, set_id)
        variant_set.name = '{}-{}'.format(SETNAME, set_id)
        # TODO change to use ID
        variant_set.dataset_id = DATASET_ID
        variant_set.reference_set_id = '{}-{}'.format(REFERENCE_SET_BASE, set_id)
        brca_meta(variant_set.metadata, dataset_id)
        response.variant_sets.extend([variant_set])
    return JsonResponse(json_format._MessageToJsonObject(response, True))

def brca_meta(Metadata, dataset_id):
    """Auxiliary function, generates metadata fields"""
    metadata_element = variants.VariantSetMetadata()
    for key in Variant._meta.get_all_field_names():
        # TODO switch to .get_fields()
        # http://stackoverflow.com/questions/3647805/get-models-fields-in-django
        metadata_element.key = str(key)
        metadata_element.value = '-'
        metadata_element.id = '{}-{}'.format(DATASET_ID , str(key))
        metadata_element.type = Variant._meta.get_field(str(key)).get_internal_type()
        metadata_element.number = '-'
        metadata_element.description = "refer to ->{} in https://github.com/BD2KGenomics" \
                                       "/brca-website/blob/master/content/help_research.md".format(str(key))
        Metadata.extend([metadata_element])
    return Metadata


# TODO and test it
# @require_http_methods(['GET'])
# get_dataset(request, datasetId):

@require_http_methods(['GET'])
def get_variant_set(request, variantSetId):
    """/variantsets/<set id> method"""
    # TODO fix snake case
    if not variantSetId:
        return HttpResponseBadRequest(
            json.dumps(ErrorMessages['variantSetId']),
            content_type='application/json')
    dataset_id, id_ = variantSetId.split('-')
    if id_ in SET_IDS and dataset_id == DATASET_ID:
        variant_set = variants.VariantSet()
        variant_set.id = '{}-{}'.format(dataset, id_)
        variant_set.name = '{}-{}'.format(SETNAME, id)
        variant_set.dataset_id = DATASET_ID
        variant_set.reference_set_id = '{}-{}'.format(REFERENCE_SET_BASE, id_)
        brca_meta(variant_set.metadata, id_)
        resp = json_format._MessageToJsonObject(variant_set, True)
        return JsonResponse(resp)
    else:
        return JsonResponse({'Invalid Set Id': variantSetId}, status=404)


@require_http_methods(['POST'])
def search_datasets(request):
    """/datasets/search method request handler"""
    # TODO paging datasets
    # TODO no validation in request body, bug if no request content
    request_dict = json.loads(request.body)
    page_size = request_dict.get('pageSize', DEFAULT_PAGE_SIZE)
    page_token = request_dict.get('nextPageToken', '0')
    response = metadata_service.SearchDatasetsResponse()
    dataset = metadata.Dataset()
    dataset.name = SETNAME
    dataset.id = DATASET_ID
    #dta_resp.info[SETNAME].append("This set contains variants as stored and mantained by the brca-exchange project")
    dataset.description = 'Variants observed in brca-exchange project'
    response.datasets.extend([dataset])
    response.next_page_token = page_token
    return JsonResponse(json_format._MessageToJsonObject(response, False))

@require_http_methods(['GET', 'POST'])
def varsetId_empty_catcher(request):
    """Error URL catcher methods"""
    return HttpResponseBadRequest(json.dumps(ErrorMessages['emptyBody']), content_type='application/json')

@require_http_methods(['GET', 'POST'])
def empty_varId_catcher(request):
    return HttpResponseBadRequest(json.dumps(ErrorMessages['emptyBody']), content_type='application/json')

# TODO add errors for the not found cases and use them
ErrorMessages = {'emptyBody' :{'status_code': 400, 'message' : 'Invalid request: empty request'},
                 'variantSetId' : {'status_code': 400, 'message': 'Invalid request: please provide a variantSetId'},
                 'referenceName': {'status_code': 400, 'message': 'Invalid request: please provide a referenceName'},
                 'start': {'status_code' : 400, 'message': 'Invalid request: please provide a start position'},
                 'end' : {'status_code' :400, 'message': 'Invalid request: please provide an end position'},
                 'datasetId': {'status_code' : 400, 'message': 'Invalid request: please provide a datasetId'},
                 'variantId': {'status_code' : 400, 'message': 'Invalid request: please provide a variantId'}}

# The display name for the variant set.
SETNAME = 'brca-exchange-variants'

# The string identify the reference set. Currently for display only.
REFERENCE_SET_BASE = 'Genomic-Coordinate'

# The identifier for the dataset
DATASET_ID = 'brca'

# The name of the dataset for display
DATASET_NAME = 'brca-exchange'

# The list of reference genomes used to switch between variant sets.
SET_IDS = ['hg36', 'hg37', 'hg38']

# The list of reference names to be served
REFERENCE_NAMES = ['chr13', 'chr17', '13', '17']

# When no pagesize is specified pages of this length will be returned.
DEFAULT_PAGE_SIZE = 3

# Need to implement function that filters elements by increasing and decreasing integer values
