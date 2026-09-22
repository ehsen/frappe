# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

# Tree (Hierarchical) Nested Set Model (nsm)
#
# To use the nested set model,
# use the following pattern
# 1. name your parent field as "parent_item_group" if not have a property nsm_parent_field as your field name in the document class
# 2. have a field called "old_parent" in your fields list - this identifies whether the parent has been changed
# 3. call update_nsm(doc_obj) in the on_update method

# ------------------------------------------
from collections.abc import Iterator

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Order
from frappe.query_builder.functions import Coalesce, Max
from frappe.query_builder.terms import SubQuery
from frappe.query_builder.utils import DocType


class NestedSetRecursionError(frappe.ValidationError):
	pass


class NestedSetMultipleRootsError(frappe.ValidationError):
	pass


class NestedSetChildExistsError(frappe.ValidationError):
	pass


class NestedSetInvalidMergeError(frappe.ValidationError):
	pass


# called in the on_update method
def update_nsm(doc):
	# get fields, data from the DocType
	old_parent_field = "old_parent"
	parent_field = "parent_" + frappe.scrub(doc.doctype)

	if hasattr(doc, "nsm_parent_field"):
		parent_field = doc.nsm_parent_field
	if hasattr(doc, "nsm_oldparent_field"):
		old_parent_field = doc.nsm_oldparent_field

	parent, old_parent = doc.get(parent_field) or None, doc.get(old_parent_field) or None

	# has parent changed (?) or parent is None (root)
	if not doc.lft and not doc.rgt:
		update_add_node(doc, parent or "", parent_field)
	elif old_parent != parent:
		update_move_node(doc, parent_field)

	# set old parent
	doc.set(old_parent_field, parent)
	frappe.db.set_value(doc.doctype, doc.name, old_parent_field, parent or "", update_modified=False)
	frappe.clear_document_cache(doc.doctype)

	doc.reload()


def _shift_matched_rows(doctype, filters, sets):
	"""P2.1: a table-range UPDATE with a self-referential arithmetic SET
	(`SET rgt = rgt + 2 WHERE rgt >= right`) hangs the SurrealDB server
	(repro'd via the root CLI). Matched rows are read flat with a SELECT
	(reads are safe) and shifted per record instead.
	"""
	Table = DocType(doctype)
	for name in frappe.get_all(doctype, filters=filters, pluck="name", limit_page_length=0):
		query = frappe.qb.update(Table)
		for column, value in sets.items():
			query = query.set(getattr(Table, column), value)
		query.where(Table.name == name).run()


def update_add_node(doc, parent, parent_field):
	"""
	insert a new node
	"""
	doctype = doc.doctype
	name = doc.name
	Table = DocType(doctype)

	# get the last sibling of the parent
	if parent:
		left, right = frappe.db.get_value(doctype, {"name": parent}, ["lft", "rgt"], for_update=True)
		validate_loop(doc.doctype, doc.name, left, right)
	else:  # root
		right = (
			frappe.qb.from_(Table)
			.select(Coalesce(Max(Table.rgt), 0) + 1)
			.where(Coalesce(Table[parent_field], "") == "")
			.run(pluck=True)[0]
		)

	right = right or 1

	# update all on the right (P2.1: per-record shifts, see _shift_matched_rows)
	_shift_matched_rows(doctype, {"rgt": [">=", right]}, {"rgt": Table.rgt + 2})
	_shift_matched_rows(doctype, {"lft": [">=", right]}, {"lft": Table.lft + 2})

	if frappe.qb.from_(Table).select("*").where((Table.lft == right) | (Table.rgt == right + 1)).run():
		frappe.throw(_("Nested set error. Please contact the Administrator."))

	# update index of new node
	frappe.qb.update(Table).set(Table.lft, right).set(Table.rgt, right + 1).where(Table.name == name).run()
	return right


def update_move_node(doc: Document, parent_field: str):
	parent: str = doc.get(parent_field)
	Table = DocType(doc.doctype)

	if parent:
		new_parent = (
			frappe.qb.from_(Table)
			.select(Table.lft, Table.rgt)
			.where(Table.name == parent)
			.for_update()
			.run(as_dict=True)[0]
		)

		validate_loop(doc.doctype, doc.name, new_parent.lft, new_parent.rgt)

	# move to dark side (P2.1: per-record shifts, see _shift_matched_rows)
	_shift_matched_rows(
		doc.doctype,
		[["lft", ">=", doc.lft], ["rgt", "<=", doc.rgt]],
		{"lft": -Table.lft, "rgt": -Table.rgt},
	)

	# shift left
	diff = doc.rgt - doc.lft + 1
	_shift_matched_rows(doc.doctype, {"lft": [">", doc.rgt]}, {"lft": Table.lft - diff, "rgt": Table.rgt - diff})

	# shift left rgts of ancestors whose only rgts must shift
	_shift_matched_rows(
		doc.doctype, [["lft", "<", doc.lft], ["rgt", ">", doc.rgt]], {"rgt": Table.rgt - diff}
	)

	if parent:
		# re-query value due to computation above
		new_parent = (
			frappe.qb.from_(Table)
			.select(Table.lft, Table.rgt)
			.where(Table.name == parent)
			.for_update()
			.run(as_dict=True)[0]
		)

		# set parent lft, rgt
		frappe.qb.update(Table).set(Table.rgt, Table.rgt + diff).where(Table.name == parent).run()

		# shift right at new parent
		_shift_matched_rows(
			doc.doctype, {"lft": [">", new_parent.rgt]}, {"lft": Table.lft + diff, "rgt": Table.rgt + diff}
		)

		# shift right rgts of ancestors whose only rgts must shift
		_shift_matched_rows(
			doc.doctype,
			[["lft", "<", new_parent.lft], ["rgt", ">", new_parent.rgt]],
			{"rgt": Table.rgt + diff},
		)

		new_diff = new_parent.rgt - doc.lft
	else:
		# new root
		max_rgt = frappe.qb.from_(Table).select(Max(Table.rgt)).run(pluck=True)[0]
		new_diff = max_rgt + 1 - doc.lft

	# bring back from dark side
	_shift_matched_rows(doc.doctype, {"lft": ["<", 0]}, {"lft": -Table.lft + new_diff, "rgt": -Table.rgt + new_diff})


@frappe.whitelist()
def rebuild_tree_for_doctype(doctype: str) -> None:
	"""Rebuild the nested set of a tree doctype on request."""
	frappe.only_for("System Manager")
	rebuild_tree(doctype)


def rebuild_tree(doctype: str) -> None:
	"""Call rebuild_node for all root nodes."""
	meta = frappe.get_meta(doctype)
	if not meta.has_field("lft") or not meta.has_field("rgt"):
		frappe.throw(
			_("Rebuilding of tree is not supported for {}").format(frappe.bold(doctype)),
			title=_("Invalid Action"),
		)

	parent_field = meta.nsm_parent_field or f"parent_{frappe.scrub(doctype)}"

	# get all roots
	right = 1
	table = DocType(doctype)
	column = getattr(table, parent_field)
	result = (
		frappe.qb.from_(table)
		.where((column == "") | (column.isnull()))
		.orderby(table.name, order=Order.asc)
		.select(table.name)
	).run()

	frappe.db.auto_commit_on_many_writes = 1

	for r in result:
		right = rebuild_node(doctype, r[0], right, parent_field)

	frappe.db.auto_commit_on_many_writes = 0


def rebuild_node(doctype, parent, left, parent_field):
	"""
	reset lft, rgt and recursive call for all children
	"""
	# the right value of this node is the left value + 1
	right = left + 1

	# get all children of this node
	table = DocType(doctype)
	column = getattr(table, parent_field)

	children = (
		(frappe.qb.from_(table).where(column == parent).select(table.name))
		.orderby(table.name, order=Order.asc)
		.run(pluck=True)
	)

	for child in children:
		right = rebuild_node(doctype, child, right, parent_field)

	# we've got the left value, and now that we've processed
	# the children of this node we also know the right value
	frappe.db.set_value(doctype, parent, {"lft": left, "rgt": right}, update_modified=False)

	# return the right value of this node + 1
	return right + 1


def validate_loop(doctype, name, lft, rgt):
	"""check if item not an ancestor (loop)"""
	if name in frappe.get_all(doctype, filters={"lft": ["<=", lft], "rgt": [">=", rgt]}, pluck="name"):
		frappe.throw(_("Item cannot be added to its own descendants"), NestedSetRecursionError)


def remove_subtree(doctype: str, name: str, throw=True):
	"""Remove doc and all its children.

	WARN: This does not run any controller hooks for deletion and deletes them with raw SQL query.
	"""
	frappe.has_permission(doctype, ptype="delete", throw=throw)

	# Determine the `lft` and `rgt` of the subtree to be removed.
	lft, rgt = frappe.db.get_value(doctype, name, ["lft", "rgt"])

	# Delete the subtree by removing all nodes whose values for `lft` and `rgt`
	# lie within above values or match them.
	frappe.db.delete(doctype, {"lft": (">=", lft), "rgt": ("<=", rgt)})

	# The width of the subtree is calculated as the difference between `rgt` and
	# `lft` plus 1.
	width = rgt - lft + 1

	# All `lft` and `rgt` values, that are greater than the `rgt` of the removed
	# subtree, must be reduced by the width of the subtree.
	# (P2.1: per-record shifts, see _shift_matched_rows)
	Table = DocType(doctype)
	_shift_matched_rows(doctype, {"lft": [">", rgt]}, {"lft": Table.lft - width})
	_shift_matched_rows(doctype, {"rgt": [">", rgt]}, {"rgt": Table.rgt - width})

	frappe.clear_document_cache(doctype)


class NestedSet(Document):
	def __setup__(self):
		if self.meta.get("nsm_parent_field"):
			self.nsm_parent_field = self.meta.nsm_parent_field

	def after_insert(self):
		if (
			frappe.flags.in_import
			or frappe.flags.in_patch
			or frappe.flags.in_migrate
			or frappe.flags.in_install
		):
			return

		# Clear user permissions cache, otherwise user can't access the new document
		if frappe.db.exists("User Permission", {"user": frappe.session.user, "allow": self.doctype}):
			frappe.cache.hdel("user_permissions", frappe.session.user)

	def on_update(self):
		update_nsm(self)
		self.validate_ledger()

	def on_trash(self, allow_root_deletion=False):
		"""
		Runs on deletion of a document/node

		:param allow_root_deletion: used for allowing root document deletion (DEPRECATED)
		"""

		if not getattr(self, "nsm_parent_field", None):
			self.nsm_parent_field = frappe.scrub(self.doctype) + "_parent"

		parent = self.get(self.nsm_parent_field)
		if not parent and not getattr(self, "allow_root_deletion", True):
			frappe.throw(_("Root {0} cannot be deleted").format(_(self.doctype)))

		# cannot delete non-empty group
		self.validate_if_child_exists()

		self.set(self.nsm_parent_field, "")

		try:
			update_nsm(self)
		except frappe.DoesNotExistError:
			if self.flags.on_rollback:
				frappe.clear_last_message()
			else:
				raise

	def validate_if_child_exists(self):
		has_children = frappe.db.count(self.doctype, filters={self.nsm_parent_field: self.name})
		if has_children:
			frappe.throw(
				_("Cannot delete {0} as it has child nodes").format(self.name), NestedSetChildExistsError
			)

	def before_rename(self, olddn, newdn, merge=False, group_fname="is_group"):
		if merge and hasattr(self, group_fname):
			is_group = frappe.db.get_value(self.doctype, newdn, group_fname)
			if self.get(group_fname) != is_group:
				frappe.throw(
					_("Merging is only possible between Group-to-Group or Leaf Node-to-Leaf Node"),
					NestedSetInvalidMergeError,
				)

	def after_rename(self, olddn, newdn, merge=False):
		if not self.nsm_parent_field:
			parent_field = "parent_" + self.doctype.replace(" ", "_").lower()
		else:
			parent_field = self.nsm_parent_field

		# set old_parent for children
		frappe.db.set_value(
			self.doctype,
			{parent_field: newdn},
			{"old_parent": newdn},
			update_modified=False,
		)

		if merge:
			rebuild_tree(self.doctype)

	def validate_one_root(self):
		if not self.get(self.nsm_parent_field):
			if self.get_root_node_count() > 1:
				frappe.throw(_("""Multiple root nodes not allowed."""), NestedSetMultipleRootsError)

	def get_root_node_count(self):
		return frappe.db.count(self.doctype, {self.nsm_parent_field: ""})

	def validate_ledger(self, group_identifier="is_group"):
		if hasattr(self, group_identifier) and not bool(self.get(group_identifier)):
			if frappe.get_all(self.doctype, {self.nsm_parent_field: self.name, "docstatus": ("!=", 2)}):
				frappe.throw(
					_("{0} {1} cannot be a leaf node as it has children").format(_(self.doctype), self.name)
				)

	def get_ancestors(self):
		return get_ancestors_of(self.doctype, self.name)

	def get_parent(self) -> "NestedSet":
		"""Return the parent Document."""
		parent_name = self.get(self.nsm_parent_field)
		if parent_name:
			return frappe.get_doc(self.doctype, parent_name)

	def get_children(self) -> Iterator["NestedSet"]:
		"""Return a generator that yields child Documents."""
		child_names = frappe.get_list(self.doctype, filters={self.nsm_parent_field: self.name}, pluck="name")
		for name in child_names:
			yield frappe.get_doc(self.doctype, name)


def get_root_of(doctype):
	"""Get root element of a DocType with a tree structure

	P2.1: the correlated Count subquery (`node_query == 0`) is a P1.6 gap;
	the root (a node with no proper ancestor and a non-empty lft/rgt) is
	found over a flat single-table read stitched in Python.
	"""
	rows = frappe.get_all(doctype, fields=["name", "lft", "rgt"])
	for row in rows:
		lft, rgt = row["lft"], row["rgt"]
		if lft is None or rgt is None or not rgt > lft:
			continue
		has_ancestor = any(
			other["lft"] is not None
			and other["rgt"] is not None
			and other["lft"] < lft
			and other["rgt"] > rgt
			for other in rows
		)
		if not has_ancestor:
			return row["name"]

	return None


def get_ancestors_of(doctype, name, order_by="lft desc", limit=None):
	"""Get ancestor elements of a DocType with a tree structure"""
	lft, rgt = frappe.db.get_value(doctype, name, ["lft", "rgt"])

	return frappe.get_all(
		doctype,
		{"lft": ["<", lft], "rgt": [">", rgt]},
		"name",
		order_by=order_by,
		limit_page_length=limit,
		pluck="name",
	)


def get_descendants_of(doctype, name, order_by="lft desc", limit=None, ignore_permissions=False):
	"""Return descendants of the current record"""
	lft, rgt = frappe.db.get_value(doctype, name, ["lft", "rgt"])

	if rgt - lft <= 1:
		return []

	return frappe.get_list(
		doctype,
		{"lft": [">", lft], "rgt": ["<", rgt]},
		"name",
		order_by=order_by,
		limit_page_length=limit,
		ignore_permissions=ignore_permissions,
		pluck="name",
	)
