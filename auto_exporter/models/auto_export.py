import csv
from io import StringIO
from typing import Any, List

from odoo import _, api, fields, models


class AutoExport(models.Model):  # TODO Check
    _name = "auto_export"  # TODO

    name = fields.Char(
        required=True,
    )
    export_id = fields.Many2one(
        comodel_name="ir.exports",
        string="Export Template",
        required=True,
    )
    model_id = fields.Many2one(
        comodel_name="ir.model",
        string="Model",
        required=True,
        compute="_compute_model_id",
        ondelete="cascade",
    )
    cron_id = fields.Many2one(
        comodel_name="ir.cron",
        string="Cron",
        readonly=True,
    )
    ftp_server_id = fields.Many2one(  # TODO add more ways to save
        comodel_name="ftp_server",
        string="FTP Server",
        required=True,
    )

    def _get_cron_name(self):
        return f"Auto Export: {self.name}"

    def _create_cron(self):
        self.cron_id = self.env["ir.cron"].create(
            {
                "name": self._get_cron_name(),
                "model_id": self.env["ir.model"].search([("model", "=", self._name)]).id,
                "state": "code",
                "code": f"model.export_by_id({self.id})",
                "interval_number": 1,
                "interval_type": "days",
                "numbercall": -1,
            }
        )

    def export_by_id(self, id):
        return self.browse(id).export()

    @api.model
    def create(self, vals):
        res = super().create(vals)
        res._create_cron()
        return res

    @api.depends("export_id.resource")
    def _compute_model_id(self):
        for auto_export in self:
            auto_export.model_id = self.env["ir.model"]._get(auto_export.export_id.resource)

    def _get_field_names(self):
        return self.export_id.export_fields.mapped("name")

    def _get_records(self):
        Model = self.env[self.model_id.model].with_context(auto_exporter_fill_missing_fields=True)
        return Model.search([])  # TODO domain

    def _get_datas(self, records) -> List[List[Any]]:
        return [self._get_field_names()] + records.export_data(self._get_field_names()).get(
            "datas", []
        )

    def _get_file_name(self):
        now = fields.Datetime.now()
        return f"{self.name}_{now.isoformat()}.csv"

    def _get_file_content(self, datas) -> bytes:
        with StringIO() as file:
            writer = csv.writer(file)
            writer.writerows(datas)
            return file.getvalue().encode("UTF-8")

    def save(self, datas):
        file_name = self._get_file_name()
        file_content = self._get_file_content(datas)
        self.ftp_server_id.upload(file_name, file_content)

    def export(self):
        records = self._get_records()
        datas = self._get_datas(records)
        self.save(datas)
