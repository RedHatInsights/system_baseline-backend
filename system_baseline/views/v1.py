from flask import Blueprint, request, current_app, Response
from http import HTTPStatus
import json
import jsonpatch
import jsonpointer

from sqlalchemy.orm.session import make_transient

from kerlescan import view_helpers
from kerlescan import profile_parser
from kerlescan.exceptions import HTTPError, ItemNotReturned
from kerlescan.inventory_service_interface import fetch_systems_with_profiles
from kerlescan.service_interface import get_key_from_headers

from system_baseline import metrics, app_config, validators
from system_baseline.version import app_version
from system_baseline.models import SystemBaseline, db
from system_baseline.exceptions import FactValidationError

section = Blueprint("v1", __name__)

pagination_link_template = "%s?limit=%s&offset=%s&order_by=%s&order_how=%s"

FACTS_MAXSIZE = 2 ** 19  # 512KB


def _create_first_link(path, limit, offset, count, order_by, order_how):
    first_link = pagination_link_template % (path, limit, 0, order_by, order_how)
    return first_link


def _create_previous_link(path, limit, offset, count, order_by, order_how):
    # if we are at the beginning, do not create a previous link
    if offset == 0 or offset - limit < 0:
        return _create_first_link(path, limit, offset, count, order_by, order_how)
    previous_link = pagination_link_template % (
        request.path,
        limit,
        offset - limit,
        order_by,
        order_how,
    )
    return previous_link


def _create_next_link(path, limit, offset, count, order_by, order_how):
    # if we are at the end, do not create a next link
    if limit + offset >= count:
        return _create_last_link(path, limit, offset, count, order_by, order_how)
    next_link = pagination_link_template % (
        request.path,
        limit,
        limit + offset,
        order_by,
        order_how,
    )
    return next_link


def _create_last_link(path, limit, offset, count, order_by, order_how):
    final_offset = count - limit if (count - limit) >= 0 else 0
    last_link = pagination_link_template % (
        path,
        limit,
        final_offset,
        order_by,
        order_how,
    )
    return last_link


def _build_paginated_baseline_list_response(
    count, limit, offset, order_by, order_how, baseline_list, withhold_facts=False
):
    json_baseline_list = [
        baseline.to_json(withhold_facts=withhold_facts) for baseline in baseline_list
    ]
    link_params = {
        "path": request.path,
        "limit": limit,
        "offset": offset,
        "order_by": order_by,
        "order_how": order_how,
        "count": count,
    }
    json_output = {
        "meta": {"count": count, "total_available": _get_total_baseline_count()},
        "links": {
            "first": _create_first_link(**link_params),
            "next": _create_next_link(**link_params),
            "previous": _create_previous_link(**link_params),
            "last": _create_last_link(**link_params),
        },
        "data": json_baseline_list,
    }

    return _build_json_response(json_output)


def _build_json_response(json_data, status=200):
    return Response(json.dumps(json_data), status=status, mimetype="application/json")


def _get_total_baseline_count():
    """
    return a count of total number of baselines available for an account
    """
    account_number = view_helpers.get_account_number(request)
    query = SystemBaseline.query.filter(SystemBaseline.account == account_number)
    return query.count()


def get_version():
    """
    return the service version
    """
    return {"version": app_version}


@metrics.baseline_fetch_requests.time()
@metrics.api_exceptions.count_exceptions()
def get_baselines_by_ids(baseline_ids, limit, order_by, order_how, offset):
    """
    return a list of baselines given their ID
    """
    _validate_uuids(baseline_ids)
    account_number = view_helpers.get_account_number(request)
    query = SystemBaseline.query.filter(
        SystemBaseline.account == account_number, SystemBaseline.id.in_(baseline_ids)
    )

    query = _create_ordering(order_by, order_how, query)
    query = query.limit(limit).offset(offset)
    query_results = query.all()

    return _build_paginated_baseline_list_response(
        len(query_results),
        limit,
        offset,
        order_by,
        order_how,
        query_results,
        withhold_facts=False,
    )


@metrics.baseline_delete_requests.time()
@metrics.api_exceptions.count_exceptions()
def delete_baselines_by_ids(baseline_ids):
    """
    delete a list of baselines given their ID
    """
    _validate_uuids(baseline_ids)
    _delete_baselines(baseline_ids)
    return "OK"


@metrics.baseline_delete_requests.time()
@metrics.api_exceptions.count_exceptions()
def delete_baselines_by_list(baseline_ids_list):
    """
    delete a list of baselines given their IDs as a list
    """
    baseline_ids = baseline_ids_list["baseline_ids"]
    _validate_uuids(baseline_ids)
    _delete_baselines(baseline_ids)
    return "OK"


def _delete_baselines(baseline_ids):
    """
    delete baselines
    """
    account_number = view_helpers.get_account_number(request)
    query = SystemBaseline.query.filter(
        SystemBaseline.account == account_number, SystemBaseline.id.in_(baseline_ids)
    )
    query.delete(synchronize_session="fetch")
    db.session.commit()


def _create_ordering(order_by, order_how, query):
    """
    helper method to set ordering on query. `order_by` and `order_how` are
    guaranteed to be populated as "DESC" or "ASC" via the openapi definition.
    """
    if order_by == "display_name":
        if order_how == "DESC":
            query = query.order_by(SystemBaseline.display_name.desc())
        elif order_how == "ASC":
            query = query.order_by(SystemBaseline.display_name.asc())
    elif order_by == "created_on":
        if order_how == "DESC":
            query = query.order_by(SystemBaseline.created_on.desc())
        elif order_how == "ASC":
            query = query.order_by(SystemBaseline.created_on.asc())
    elif order_by == "updated":
        if order_how == "DESC":
            query = query.order_by(SystemBaseline.modified_on.desc())
        elif order_how == "ASC":
            query = query.order_by(SystemBaseline.modified_on.asc())

    return query


@metrics.baseline_fetch_all_requests.time()
@metrics.api_exceptions.count_exceptions()
def get_baselines(limit, offset, order_by, order_how, display_name=None):
    """
    return a list of baselines given their ID
    """
    account_number = view_helpers.get_account_number(request)
    query = SystemBaseline.query.filter(SystemBaseline.account == account_number)

    if display_name:
        query = query.filter(
            SystemBaseline.display_name.contains(display_name, autoescape=True)
        )

    query = _create_ordering(order_by, order_how, query)

    query = query.limit(limit).offset(offset)
    query_results = query.all()

    return _build_paginated_baseline_list_response(
        len(query_results),
        limit,
        offset,
        order_by,
        order_how,
        query_results,
        withhold_facts=True,
    )


def group_baselines(baseline):
    """
    return a grouped baseline
    """

    def _get_group_name(name):
        n, _, _ = name.partition(".")
        return n

    def _get_value_name(name):
        _, _, n = name.partition(".")
        return n

    def _find_group(name):
        for group in grouped_baseline:
            if group["name"] == name:
                return group

    # build out group names
    group_names = {_get_group_name(b["name"]) for b in baseline if "." in b["name"]}
    grouped_baseline = []
    for group_name in group_names:
        grouped_baseline.append({"name": group_name, "values": []})

    # populate groups
    for fact in baseline:
        if "." in fact["name"]:
            group = _find_group(_get_group_name(fact["name"]))
            fact["name"] = _get_value_name(fact["name"])
            group["values"].append(fact)
        else:
            grouped_baseline.append(fact)

    return grouped_baseline


def get_event_counters():
    """
    small helper to create a dict of event counters
    """
    return {
        "systems_compared_no_sysprofile": metrics.inventory_service_no_profile,
        "inventory_service_requests": metrics.inventory_service_requests,
        "inventory_service_exceptions": metrics.inventory_service_exceptions,
    }


@metrics.baseline_create_requests.time()
@metrics.api_exceptions.count_exceptions()
def create_baseline(system_baseline_in):
    """
    create a baseline
    """
    account_number = view_helpers.get_account_number(request)

    if "values" in system_baseline_in and "value" in system_baseline_in:
        raise HTTPError(
            HTTPStatus.BAD_REQUEST,
            message="'values' and 'value' cannot both be defined for system baseline",
        )

    _check_for_existing_display_name(system_baseline_in["display_name"], account_number)

    baseline_facts = []
    if "baseline_facts" in system_baseline_in:
        baseline_facts = system_baseline_in["baseline_facts"]
    elif "inventory_uuid" in system_baseline_in:
        _validate_uuids([system_baseline_in["inventory_uuid"]])
        auth_key = get_key_from_headers(request.headers)
        try:
            system_with_profile = fetch_systems_with_profiles(
                [system_baseline_in["inventory_uuid"]],
                auth_key,
                current_app.logger,
                get_event_counters(),
            )[0]
        except ItemNotReturned:
            raise HTTPError(
                HTTPStatus.BAD_REQUEST,
                message="inventory UUID %s not available"
                % system_baseline_in["inventory_uuid"],
            )

        system_name = profile_parser.get_name(system_with_profile)
        parsed_profile = profile_parser.parse_profile(
            system_with_profile["system_profile"], system_name, current_app.logger
        )
        facts = []
        for fact in parsed_profile:
            if fact not in ["id", "name"] and parsed_profile[fact] not in [
                "N/A",
                "None",
                None,
            ]:
                facts.append({"name": fact, "value": parsed_profile[fact]})

        baseline_facts = group_baselines(facts)

    try:
        _validate_facts(baseline_facts)
    except FactValidationError as e:
        raise HTTPError(HTTPStatus.BAD_REQUEST, message=e.message)

    baseline = SystemBaseline(
        account=account_number,
        display_name=system_baseline_in["display_name"],
        baseline_facts=baseline_facts,
    )
    baseline.baseline_facts = _sort_baseline_facts(baseline.baseline_facts)
    db.session.add(baseline)
    db.session.commit()  # commit now so we get a created/updated time before json conversion

    return baseline.to_json()


def _check_for_existing_display_name(display_name, account_number):
    """
    check to see if a display name already exists for an account, and raise an exception if so.
    """

    query = SystemBaseline.query.filter(
        SystemBaseline.account == account_number,
        SystemBaseline.display_name == display_name,
    )

    if query.count() > 0:
        raise HTTPError(
            HTTPStatus.BAD_REQUEST,
            message="display_name '%s' already used for this account" % display_name,
        )


def _validate_uuids(uuids):
    """
    helper method to raise user-friendly exception on UUID format errors
    """
    try:
        validators.check_uuids(uuids)
    except ValueError:
        raise HTTPError(
            HTTPStatus.BAD_REQUEST,
            message="malformed UUID requested (%s)" % ",".join(uuids),
        )

    uuid_set = set(uuids)
    if len(uuid_set) < len(uuids):
        raise HTTPError(
            HTTPStatus.BAD_REQUEST, message="duplicate UUIDs specified in request"
        )


def _sort_baseline_facts(baseline_facts):
    """
    helper method to sort baseline facts by name before saving to the DB.
    """
    sorted_baseline_facts = sorted(
        baseline_facts, key=lambda fact: fact["name"].lower()
    )
    for fact in sorted_baseline_facts:
        if "values" in fact:
            fact["values"] = sorted(
                fact["values"], key=lambda fact: fact["name"].lower()
            )
    return sorted_baseline_facts


def _sort_baseline_facts(baseline_facts):
    """
    helper method to sort baseline facts by name before saving to the DB.
    """
    sorted_baseline_facts = sorted(
        baseline_facts, key=lambda fact: fact["name"].lower()
    )
    for fact in sorted_baseline_facts:
        if "values" in fact:
            fact["values"] = sorted(
                fact["values"], key=lambda fact: fact["name"].lower()
            )
    return sorted_baseline_facts


@metrics.baseline_create_requests.time()
@metrics.api_exceptions.count_exceptions()
def copy_baseline_by_id(baseline_id, display_name):
    """
    create a new baseline given an existing ID
    """
    _validate_uuids([baseline_id])

    # ensure display_name is not null
    if not display_name:
        raise HTTPError(
            HTTPStatus.BAD_REQUEST, message="no value given for display_name"
        )

    account_number = view_helpers.get_account_number(request)
    _check_for_existing_display_name(display_name, account_number)

    query = SystemBaseline.query.filter(
        SystemBaseline.account == account_number, SystemBaseline.id == baseline_id
    )

    copy_baseline = query.first_or_404()
    db.session.expunge(copy_baseline)
    make_transient(copy_baseline)
    copy_baseline.id = None
    copy_baseline.created_on = None
    copy_baseline.modified_on = None
    copy_baseline.display_name = display_name
    db.session.add(copy_baseline)
    db.session.commit()
    return copy_baseline.to_json()


def update_baseline(baseline_id, system_baseline_patch):
    """
    update a baseline
    """
    _validate_uuids([baseline_id])

    account_number = view_helpers.get_account_number(request)

    # this query is a bit different than what's in _check_for_existing_display_name,
    # since it's OK if the display name is used by the baseline we are updating
    existing_display_name_query = SystemBaseline.query.filter(
        SystemBaseline.account == account_number,
        SystemBaseline.id != baseline_id,
        SystemBaseline.display_name == system_baseline_patch["display_name"],
    )

    if existing_display_name_query.count() > 0:
        raise HTTPError(
            HTTPStatus.BAD_REQUEST,
            message="display_name '%s' already used for this account"
            % system_baseline_patch["display_name"],
        )

    query = SystemBaseline.query.filter(
        SystemBaseline.account == account_number, SystemBaseline.id == baseline_id
    )
    baseline = query.first_or_404()

    try:
        updated_facts = jsonpatch.apply_patch(
            baseline.baseline_facts, system_baseline_patch["facts_patch"]
        )
        _validate_facts(updated_facts)
        baseline.baseline_facts = updated_facts
    except FactValidationError as e:
        raise HTTPError(HTTPStatus.BAD_REQUEST, message=e.message)
    except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException):
        raise HTTPError(
            HTTPStatus.BAD_REQUEST, message="unable to apply patch to baseline"
        )

    baseline.display_name = system_baseline_patch["display_name"]

    baseline.baseline_facts = _sort_baseline_facts(baseline.baseline_facts)
    db.session.add(baseline)
    db.session.commit()

    # pull baseline again so we have the correct updated timestamp and fact count
    query = SystemBaseline.query.filter(
        SystemBaseline.account == account_number, SystemBaseline.id == baseline_id
    )
    return [query.first().to_json()]


def _validate_facts(facts):
    """
    helper to run common validations
    """
    validators.check_facts_length(facts)
    validators.check_for_duplicate_names(facts)
    validators.check_for_empty_name_values(facts)
    validators.check_for_value_values(facts)
    validators.check_name_value_length(facts)


@section.before_app_request
def log_username():
    view_helpers.log_username(current_app.logger, request)


@section.before_app_request
def ensure_entitled():
    return view_helpers.ensure_entitled(
        request, app_config.get_app_name(), current_app.logger
    )


@section.before_app_request
def ensure_account_number():
    return view_helpers.ensure_account_number(request, current_app.logger)
