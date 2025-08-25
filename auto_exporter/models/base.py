import collections
import contextlib
from collections import defaultdict

from odoo import models
from odoo.models import BaseModel
from odoo.tools import unique, groupby


class BaseModelExtend(models.AbstractModel):
    _inherit = "base"

    # Odoo official
    def _export_rows(self, fields, *, _is_toplevel_call=True):
        """ Export fields of the records in ``self``.

        :param list fields: list of lists of fields to traverse
        :param bool _is_toplevel_call:
            used when recursing, avoid using when calling from outside
        :return: list of lists of corresponding values
        """
        import_compatible = self.env.context.get('import_compat', True)
        lines = []


        if not _is_toplevel_call:
            # {properties_field: {property_name: [property_type, {record_id: value}]}}
            cache_properties = self.env.cr.cache['export_properties_cache']
        else:
            cache_properties = self.env.cr.cache['export_properties_cache'] = defaultdict(dict)

            def fill_properties_cache(records, fnames_by_path, fname):
                """ Fill the cache for the ``fname`` properties field and return it """
                cache_properties_field = cache_properties[records._fields[fname]]

                # read properties to have all the logic of Properties.convert_to_read_multi
                for row in records.read([fname]):
                    properties = row[fname]
                    if not properties:
                        continue
                    rec_id = row['id']

                    for property in properties:
                        current_prop_name = property['name']
                        if f"{fname}.{current_prop_name}" not in fnames_by_path:
                            continue

                        property_type = property['type']
                        if current_prop_name not in cache_properties_field:
                            cache_properties_field[current_prop_name] = [property_type, {}]

                        __, cache_by_id = cache_properties_field[current_prop_name]
                        if rec_id in cache_by_id:
                            continue

                        value = property.get('value')
                        if property_type in ('many2one', 'many2many'):
                            if not isinstance(value, list):
                                value = [value] if value else []
                            value = self.env[property['comodel']].browse([val[0] for val in value])
                        elif property_type == 'tags' and value:
                            value = ",".join(
                                next(iter(tag[1] for tag in property['tags'] if tag[0] == v), '')
                                for v in value
                            )
                        elif property_type == 'selection':
                            value = dict(property['selection']).get(value, '')
                        cache_by_id[rec_id] = value

            def fetch_fields(records, field_paths):
                """ Fill the cache of ``records`` for all ``field_paths`` recursively included properties"""
                if not records:
                    return

                fnames_by_path = dict(groupby(
                    [path for path in field_paths if path and path[0] not in ('id', '.id')],
                    lambda path: path[0],
                ))

                # Fetch needed fields (remove '.property_name' part)
                fnames = list(unique(fname.split('.')[0] for fname in fnames_by_path))
                records.fetch(fnames)
                # Fill the cache of the properties field
                for fname in fnames:
                    field = records._fields[fname]
                    if field.type == 'properties':
                        fill_properties_cache(records, fnames_by_path, fname)

                # Call it recursively for relational field (included property relational field)
                for fname, paths in fnames_by_path.items():
                    if '.' in fname:  # Properties field
                        fname, prop_name = fname.split('.')
                        field = records._fields[fname]
                        assert field.type == 'properties' and prop_name

                        property_type, property_cache = cache_properties[field].get(prop_name, ('char', None))
                        if property_type not in ('many2one', 'many2many') or not property_cache:
                            continue
                        model = next(iter(property_cache.values())).browse()
                        subrecords = model.union(*[property_cache[rec_id] for rec_id in records.ids if rec_id in property_cache])
                    else:  # Normal field
                        field = records._fields[fname]
                        if not field.relational:
                            continue
                        subrecords = records[fname]

                    paths = [path[1:] or ['display_name'] for path in paths]
                    fetch_fields(subrecords, paths)

            fetch_fields(self, fields)

        for record in self:
            # main line of record, initially empty
            current = [''] * len(fields)
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

                if name == '.id':
                    current[i] = str(record.id)
                elif name == 'id':
                    current[i] = (record._name, record.id)
                else:
                    prop_name = None
                    if '.' in name:  # Properties field
                        fname, prop_name = name.split('.')
                        field = record._fields[fname]
                        field_type, cache_value = cache_properties[field].get(prop_name, ('char', None))
                        value = cache_value.get(record.id, '') if cache_value else ''
                    else:  # Normal field
                        field = record._fields[name]
                        field_type = field.type
                        value = record[name]

                    # this part could be simpler, but it has to be done this way
                    # in order to reproduce the former behavior
                    if not isinstance(value, BaseModel):
                        current[i] = field.convert_to_export(value, record)

                    elif import_compatible and field_type == 'reference':
                        current[i] = f"{value._name},{value.id}"

                    else:
                        primary_done.append(name)
                        # recursively export the fields that follow name; use
                        # 'display_name' where no subfield is exported
                        fields2 = [(p[1:] or ['display_name'] if p and p[0] == name else [])
                                   for p in fields]

                        # in import_compat mode, m2m should always be exported as
                        # a comma-separated list of xids or names in a single cell
                        if import_compatible and field_type == 'many2many':
                            index = None
                            # find out which subfield the user wants & its
                            # location as we might not get it as the first
                            # column we encounter
                            for name in ['id', 'name', 'display_name']:
                                with contextlib.suppress(ValueError):
                                    index = fields2.index([name])
                                    break
                            if index is None:
                                # not found anything, assume we just want the
                                # display_name in the first column
                                name = None
                                index = i

                            if name == 'id':
                                xml_ids = [xid for _, xid in value.__ensure_xml_id()]
                                current[index] = ','.join(xml_ids)
                            else:
                                current[index] = ','.join(value.mapped('display_name')) if value else ''
                            continue

                        lines2 = value._export_rows(fields2, _is_toplevel_call=False)
                        if lines2:
                            # merge first line with record's main line
                            for j, val in enumerate(lines2[0]):
                                if val or isinstance(val, (int, float)):
                                    current[j] = val
                            # append the other lines at the end
                            # Humanytek
                            value.fill_missing_fields(lines[-1], lines2[1:], fields2)
                            # Odoo official
                            lines += lines2[1:]
                        else:
                            current[i] = ''

        # if any xid should be exported, only do so at toplevel
        if _is_toplevel_call and any(f[-1] == 'id' for f in fields):
            bymodels = collections.defaultdict(set)
            xidmap = collections.defaultdict(list)
            # collect all the tuples in "lines" (along with their coordinates)
            for i, line in enumerate(lines):
                for j, cell in enumerate(line):
                    if isinstance(cell, tuple):
                        bymodels[cell[0]].add(cell[1])
                        xidmap[cell].append((i, j))
            # for each model, xid-export everything and inject in matrix
            for model, ids in bymodels.items():
                for record, xid in self.env[model].browse(ids).__ensure_xml_id():
                    for i, j in xidmap.pop((record._name, record.id)):
                        lines[i][j] = xid
            assert not xidmap, "failed to export xids for %s" % ', '.join('{}:{}' % it for it in xidmap.items())

        if _is_toplevel_call:
            self.env.cr.cache.pop('export_properties_cache', None)

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
