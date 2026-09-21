# Settings saved per user basis
# such as page_limit, filters, last_view

import json

import frappe
from frappe import safe_decode
from frappe.query_builder import Table

# dict for mapping the index and index type for the filters of different views
filter_dict = {"doctype": 0, "docfield": 1, "operator": 2, "value": 3}


def get_user_settings(doctype, for_update=False):
	user_settings = frappe.cache.hget("_user_settings", f"{doctype}::{frappe.session.user}")

	if user_settings is None:
		__UserSettings = Table("__UserSettings")
		rows = (
			frappe.qb.from_(__UserSettings)
			.select(__UserSettings.data)
			.where(__UserSettings.user == frappe.session.user)
			.where(__UserSettings.doctype == doctype)
			.run()
		)
		user_settings = (rows and rows[0][0]) or "{}"

		if not for_update:
			update_user_settings(doctype, user_settings, True)

	return user_settings or "{}"


def update_user_settings(doctype, user_settings, for_update=False):
	"""update user settings in cache"""

	if for_update:
		current = json.loads(user_settings)
	else:
		current = json.loads(get_user_settings(doctype, for_update=True))

		if isinstance(current, str):
			# corrupt due to old code, remove this in a future release
			current = {}

		current.update(user_settings)
	frappe.cache.hset("_user_settings", f"{doctype}::{frappe.session.user}", json.dumps(current))


def sync_user_settings():
	"""Sync from cache to database (called asynchronously via the browser)"""
	for key, data in frappe.cache.hgetall("_user_settings").items():
		key = safe_decode(key)
		doctype, user = key.split("::")  # WTF?
		if frappe.db.db_type == "surrealdb":
			# P1.9a: the INSERT .. ON DUPLICATE KEY UPDATE / ON CONFLICT raw SQL is engine dialect
			__UserSettings = Table("__UserSettings")
			frappe.qb.into(__UserSettings).columns("user", "doctype", "data").insert(
				user, doctype, data
			).on_duplicate_key_update(__UserSettings.data, data).run()
			continue
		frappe.db.multisql(
			{
				"mariadb": """INSERT INTO `__UserSettings`(`user`, `doctype`, `data`)
				VALUES (%s, %s, %s)
				ON DUPLICATE key UPDATE `data`=%s""",
				"*": """INSERT INTO `__UserSettings` (`user`, `doctype`, `data`)
				VALUES (%s, %s, %s)
				ON CONFLICT (`user`, `doctype`) DO UPDATE SET `data`=%s""",
			},
			(user, doctype, data, data),
			as_dict=1,
		)


@frappe.whitelist()
def save(doctype: str, user_settings: str):
	user_settings = json.loads(user_settings or "{}")
	update_user_settings(doctype, user_settings)
	return user_settings


@frappe.whitelist()
def get(doctype: str):
	return get_user_settings(doctype)


def update_user_settings_data(
	user_setting, fieldname, old, new, condition_fieldname=None, condition_values=None
):
	data = user_setting.get("data")
	if data:
		update = False
		data = json.loads(data)
		for view in ["List", "Gantt", "Kanban", "Calendar", "Image", "Inbox", "Report"]:
			view_settings = data.get(view)
			if view_settings and view_settings.get("filters"):
				view_filters = view_settings.get("filters")
				for view_filter in view_filters:
					if (
						condition_fieldname
						and view_filter[filter_dict[condition_fieldname]] != condition_values
					):
						continue
					if view_filter[filter_dict[fieldname]] == old:
						view_filter[filter_dict[fieldname]] = new
						update = True
		if update:
			__UserSettings = Table("__UserSettings")
			frappe.qb.update(__UserSettings).set("data", json.dumps(data)).where(
				__UserSettings.doctype == user_setting.doctype
			).where(__UserSettings.user == user_setting.user).run()

			# clear that user settings from the redis cache
			frappe.cache.hset("_user_settings", f"{user_setting.doctype}::{user_setting.user}", None)
