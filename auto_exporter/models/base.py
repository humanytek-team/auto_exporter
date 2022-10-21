import collections
import contextlib

from odoo import models
from odoo.models import BaseModel


class BaseModelExtend(models.AbstractModel):
    _inherit = "base"

    # Odoo oficial method
    def _export_rows(self, fields, *, _is_toplevel_call=True):
        """Export fields of the records in ``self``.

        :param fields: list of lists of fields to traverse
        :param bool _is_toplevel_call:
            used when recursing, avoid using when calling from outside
        :return: list of lists of corresponding values
        """
        import_compatible = self.env.context.get("import_compat", True)
        lines = []

        def splittor(rs):
            """Splits the self recordset in batches of 1000 (to avoid
            entire-recordset-prefetch-effects) & removes the previous batch
            from the cache after it's been iterated in full
            """
            for idx in range(0, len(rs), 1000):
                sub = rs[idx : idx + 1000]
                for rec in sub:
                    yield rec
                rs.invalidate_cache(ids=sub.ids)

        if not _is_toplevel_call:
            splittor = lambda rs: rs

        # memory stable but ends up prefetching 275 fields (???)
        for record in splittor(self):
            # main line of record, initially empty
            current = [""] * len(fields)
            lines.append(current)

            # list of primary fields followed by secondary field(s)
            primary_done = []

            # process column by column
            for i, path in enumerate(fields):
                if not path:
                    continue

                name = path[0]
                if name in primary_done:
                    continue

                if name == ".id":
                    current[i] = str(record.id)
                elif name == "id":
                    current[i] = (record._name, record.id)
                else:
                    field = record._fields[name]
                    value = record[name]

                    # this part could be simpler, but it has to be done this way
                    # in order to reproduce the former behavior
                    if not isinstance(value, BaseModel):
                        current[i] = field.convert_to_export(value, record)
                    else:
                        primary_done.append(name)
                        # recursively export the fields that follow name; use
                        # 'display_name' where no subfield is exported
                        fields2 = [
                            (p[1:] or ["display_name"] if p and p[0] == name else [])
                            for p in fields
                        ]

                        # in import_compat mode, m2m should always be exported as
                        # a comma-separated list of xids or names in a single cell
                        if import_compatible and field.type == "many2many":
                            index = None
                            # find out which subfield the user wants & its
                            # location as we might not get it as the first
                            # column we encounter
                            for name in ["id", "name", "display_name"]:
                                with contextlib.suppress(ValueError):
                                    index = fields2.index([name])
                                    break
                            if index is None:
                                # not found anything, assume we just want the
                                # name_get in the first column
                                name = None
                                index = i

                            if name == "id":
                                xml_ids = [xid for _, xid in value.__ensure_xml_id()]
                                current[index] = ",".join(xml_ids)
                            else:
                                current[index] = field.convert_to_export(value, record)
                            continue

                        lines2 = value._export_rows(fields2, _is_toplevel_call=False)
                        if lines2:
                            # merge first line with record's main line
                            for j, val in enumerate(lines2[0]):
                                if val or isinstance(val, (int, float)):
                                    current[j] = val
                            # append the other lines at the end
                            # Custom method call
                            value.fill_missing_fields(lines[-1], lines2[1:], fields2)
                            # End custom method call
                            lines += lines2[1:]
                        else:
                            current[i] = ""

        # if any xid should be exported, only do so at toplevel
        if _is_toplevel_call and any(f[-1] == "id" for f in fields):
            bymodels = collections.defaultdict(set)
            xidmap = collections.defaultdict(list)
            # collect all the tuples in "lines" (along with their coordinates)
            for i, line in enumerate(lines):
                for j, cell in enumerate(line):
                    if type(cell) is tuple:
                        bymodels[cell[0]].add(cell[1])
                        xidmap[cell].append((i, j))
            # for each model, xid-export everything and inject in matrix
            for model, ids in bymodels.items():
                for record, xid in self.env[model].browse(ids).__ensure_xml_id():
                    for i, j in xidmap.pop((record._name, record.id)):
                        lines[i][j] = xid
            assert not xidmap, "failed to export xids for %s" % ", ".join(
                "{}:{}" % it for it in xidmap.items()
            )

        return lines

    def fill_missing_fields(self, last_line, child_lines, fields):
        """Fill the child lines (from m2m or m2o) with the values of the last line (from the parent record)
        Uses env.context['auto_exporter_fill_missing_fields'] to enable/disable this feature

        :param last_line: list of values of the parent record
        :param child_lines: list of lists of values of the child records
        :param fields: list of lists of fields to traverse

        :return: None, the child_lines are modified in place
        """
        # Odoo uses an empty list to represent a field in parent not existing in child
        # ej: [[], ['name'], ['name', 'id']]
        #      ^^
        # This field must be filled with the value of the parent
        if not self.env.context.get("auto_exporter_fill_missing_fields"):
            return
        if not child_lines:
            return
        for i, field in enumerate(fields):
            if not field:
                for line in child_lines:
                    line[i] = last_line[i]
