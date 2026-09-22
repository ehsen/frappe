# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

from functools import cached_property

import frappe
from frappe.permissions import has_permission
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count
from frappe.query_builder.terms import SubQuery
from frappe.utils.data import cstr


class DeskViews:
	"""Builds the desk views (workspaces, dashboards, pages and reports) for the boot payload."""

	# allowed-entity caches refresh every six hours
	CACHE_EXPIRY = 6 * 60 * 60

	def __init__(self):
		self.pages = {}
		self.reports = {}
		self.workspaces = {}
		self.dashboards = []

	def build_entities(self):
		from frappe.desk.desktop import get_workspaces

		self.pages = self.get_allowed_pages()
		self.reports = self.get_allowed_reports()
		self.workspaces = get_workspaces()
		self.dashboards = self.get_allowed_dashboards(cache=True)
		return self

	def add_to_boot(self, bootinfo):
		bootinfo.page_info = self.pages
		bootinfo.allowed_reports = self.reports
		bootinfo.workspaces = self.workspaces
		bootinfo.dashboards = self.dashboards

	# The properties below are the per-user view-permission data read by `is_item_allowed`.
	# They load lazily and cache on the instance, so consumers don't need to populate them.

	@cached_property
	def allowed_pages(self):
		return self.get_allowed_pages(cache=True)

	@cached_property
	def allowed_reports(self):
		return self.get_allowed_reports(cache=True)

	@cached_property
	def allowed_dashboards(self):
		return {d["name"] for d in self.get_allowed_dashboards(cache=True)}

	@cached_property
	def restricted_doctypes(self):
		from frappe.cache_manager import build_domain_restricted_doctype_cache

		return frappe.cache.get_value("domain_restricted_doctypes") or build_domain_restricted_doctype_cache()

	@cached_property
	def restricted_pages(self):
		from frappe.cache_manager import build_domain_restricted_page_cache

		return frappe.cache.get_value("domain_restricted_pages") or build_domain_restricted_page_cache()

	def is_item_allowed(self, name, item_type, allowed_workspaces=None):
		"""Return whether the user may see a sidebar/workspace item.

		Relies on the consumer setting `can_read`, `allowed_pages`, `allowed_reports`,
		`allowed_dashboards`, `restricted_doctypes` and `restricted_pages` on the instance.
		"""
		if frappe.session.user == "Administrator":
			return True

		item_type = item_type.lower()

		if item_type == "doctype":
			try:
				return (
					name in (self.can_read or [])
					and name in (self.restricted_doctypes or [])
					and frappe.has_permission(name)
				)
			except frappe.DoesNotExistError:
				frappe.clear_last_message()
				return False
		if item_type == "page":
			return name in self.allowed_pages and name in self.restricted_pages
		if item_type == "report":
			return not frappe.db.get_value("Report", name, "disabled") and name in self.allowed_reports
		if item_type == "dashboard":
			return name in (self.allowed_dashboards or [])
		if item_type in ("help", "url"):
			return True
		if item_type == "workspace":
			return name in (allowed_workspaces or [])

		return False

	@classmethod
	def get_allowed_pages(cls, cache=False, user: str | None = None):
		return cls.get_user_pages_or_reports("Page", cache=cache, user=user)

	@classmethod
	def get_allowed_reports(cls, cache=False, user: str | None = None):
		return cls.get_user_pages_or_reports("Report", cache=cache, user=user)

	@classmethod
	def get_allowed_report_names(cls, cache=False, user: str | None = None) -> set[str]:
		return {cstr(report) for report in cls.get_allowed_reports(cache=cache, user=user).keys() if report}

	@classmethod
	def get_allowed_dashboards(cls, cache=False):
		"""Return dashboards the user is allowed to see.

		A dashboard is permitted when the user can access at least one of its charts or cards.
		Evaluated for the current session user and cached like pages and reports.
		"""
		from frappe.desk.doctype.dashboard.dashboard import get_permitted_cards, get_permitted_charts

		def build():
			return [
				{"name": name}
				for name in frappe.get_all("Dashboard", pluck="name")
				if get_permitted_charts(name) or get_permitted_cards(name)
			]

		return cls._allowed_entity_cache("allowed_dashboards", frappe.session.user, build, cache=cache)

	@classmethod
	def _allowed_entity_cache(cls, key, user, builder, cache=False):
		"""Return the user's allowed entities for `key`, rebuilding and re-caching on a miss.

		Pass `cache=True` to return a previously cached value instead of rebuilding. The result
		is stored per-user and expires after `CACHE_EXPIRY` seconds.
		"""
		if cache:
			cached = frappe.cache.get_value(key, user=user)
			if cached is not None:
				return cached

		value = builder()
		frappe.cache.set_value(key, value, user, cls.CACHE_EXPIRY)
		return value

	@classmethod
	def get_user_pages_or_reports(cls, parent, cache=False, user: str | None = None):
		if user is None:
			user = frappe.session.user

		return cls._allowed_entity_cache(
			"has_role:" + parent,
			user,
			lambda: cls._build_user_pages_or_reports(parent, user),
			cache=cache,
		)

	@classmethod
	def _build_user_pages_or_reports(cls, parent, user):
		roles = frappe.get_roles(user)
		has_role = {}

		page = DocType("Page")
		report = DocType("Report")

		is_report = parent == "Report"

		if is_report:
			columns = (report.name.as_("title"), report.ref_doctype, report.report_type)
		else:
			columns = (page.title.as_("title"),)

		customRole = DocType("Custom Role")
		hasRole = DocType("Has Role")
		parentTable = DocType(parent)

		def exclude_disabled_reports(query):
			return query.where(report.disabled == 0) if is_report else query

		role_field = parent.lower()

		# P2.1: the original comma-joins (customRole x hasRole x parentTable) and
		# the correlated Count subquery are P1.6 gaps in the SurrealQL translator
		# (a SELECT must have exactly one table), so the joins run as sequential
		# single-table qb reads stitched together in Python below.

		# role-matched Has Role parents (any parenttype; later steps scope the use)
		if roles:
			role_matched = (
				frappe.qb.from_(hasRole)
				.select(hasRole.parent)
				.distinct()
				.where(hasRole.role.isin(roles))
				.run(as_dict=True)
			)
		else:
			role_matched = []
		role_matched_parents = {r["parent"] for r in role_matched}

		# every custom-role attachment (any role) — drives both the custom-role
		# pass and the exclusion set the original notin(subq) encoded
		custom_role_rows = (
			frappe.qb.from_(customRole)
			.select(customRole.name, customRole.modified, customRole.ref_doctype, customRole[role_field])
			.where(customRole[role_field].isnotnull())
			.run(as_dict=True)
		)
		custom_attachments = {
			cr["name"]: {
				"modified": cr["modified"],
				"ref_doctype": cr["ref_doctype"],
				"entity": cr[role_field],
			}
			for cr in custom_role_rows
		}
		attached_entities = {info["entity"] for info in custom_attachments.values()}

		# get pages or reports set on custom role
		custom_by_entity = {}
		for cr_name, info in custom_attachments.items():
			if cr_name in role_matched_parents and info["entity"] not in custom_by_entity:
				custom_by_entity[info["entity"]] = info

		if custom_by_entity:
			pages_with_custom_roles = exclude_disabled_reports(
				frappe.qb.from_(parentTable)
				.select(parentTable.name.as_("name"), parentTable.modified, *columns)
				.where(parentTable.name.isin(list(custom_by_entity)))
			).run(as_dict=True)
		else:
			pages_with_custom_roles = []

		for p in pages_with_custom_roles:
			info = custom_by_entity[p["name"]]
			has_role[p["name"]] = {
				"modified": info["modified"],
				"title": p["title"],
				"ref_doctype": info["ref_doctype"],
			}

		# standard-role paths: role-matched parents not attached via ANY custom role
		standard_candidates = sorted(role_matched_parents - attached_entities)
		if standard_candidates:
			pages_with_standard_roles = exclude_disabled_reports(
				frappe.qb.from_(parentTable)
				.select(parentTable.name.as_("name"), parentTable.modified, *columns)
				.where(parentTable.name.isin(standard_candidates))
			).run(as_dict=True)
		else:
			pages_with_standard_roles = []

		for p in pages_with_standard_roles:
			if p["name"] not in has_role:
				has_role[p["name"]] = {"modified": p["modified"], "title": p["title"]}
				if parent == "Report":
					has_role[p["name"]].update({"ref_doctype": p["ref_doctype"]})

		# pages and reports with no role are allowed
		all_role_parent_rows = (
			frappe.qb.from_(hasRole).select(hasRole.parent).distinct().run(as_dict=True)
		)
		parents_with_any_roles = sorted({r["parent"] for r in all_role_parent_rows})

		no_role_query = (
			frappe.qb.from_(parentTable)
			.select(parentTable.name.as_("name"), parentTable.modified, *columns)
		)
		if parents_with_any_roles:
			no_role_query = no_role_query.where(parentTable.name.notin(parents_with_any_roles))
		rows_with_no_roles = exclude_disabled_reports(no_role_query).run(as_dict=True)

		for r in rows_with_no_roles:
			if r["name"] not in has_role:
				has_role[r["name"]] = {"modified": r["modified"], "title": r["title"]}
				if is_report:
					has_role[r["name"]] |= {"ref_doctype": r["ref_doctype"]}

		if is_report:
			if not has_permission("Report", user=user, print_logs=False):
				return {}

			reports = frappe.get_list(
				"Report",
				fields=["name", "report_type"],
				filters={"name": ("in", has_role.keys())},
				ignore_ifnull=True,
				user=user,
			)
			for report in reports:
				has_role[report.name]["report_type"] = report.report_type

			non_permitted_reports = set(has_role.keys()) - {r.name for r in reports}
			for r in non_permitted_reports:
				has_role.pop(r, None)

		return has_role
