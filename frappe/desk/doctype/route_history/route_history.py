# Copyright (c) 2022, Frappe Technologies and contributors
# License: MIT. See LICENSE

from typing import Any

import frappe
from frappe.deferred_insert import deferred_insert as _deferred_insert
from frappe.model.document import Document


class RouteHistory(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		route: DF.Data | None
		user: DF.Link | None
	# end: auto-generated types

	@staticmethod
	def clear_old_logs(days=30):
		from frappe.query_builder import Interval
		from frappe.query_builder.functions import Now

		table = frappe.qb.DocType("Route History")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))


@frappe.whitelist()
def deferred_insert(routes: str | list[dict[str, Any]]):
	routes = [
		{
			"user": frappe.session.user,
			"route": route.get("route"),
			"creation": route.get("creation"),
		}
		for route in frappe.parse_json(routes)
	]

	_deferred_insert("Route History", routes)


@frappe.whitelist()
def frequently_visited_links():
	# P2.1: ordering by the aggregate alias (`order_by="count desc"`) needs
	# alias-aware ORDER BY in the grouped-select path (P1.6 gap), so the
	# aggregation runs in Python over a flat single-table read.
	rows = frappe.get_all(
		"Route History",
		fields=["route", "name"],
		filters={"user": frappe.session.user},
	)
	counts: dict = {}
	for r in rows:
		counts[r["route"]] = counts.get(r["route"], 0) + 1
	routes = sorted(counts, key=lambda route: -counts[route])
	return [{"route": route, "count": counts[route]} for route in routes[:5]]
