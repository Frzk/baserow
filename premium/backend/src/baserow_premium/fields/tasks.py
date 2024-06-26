from baserow.config.celery import app
from baserow.contrib.database.fields.handler import FieldHandler
from baserow.contrib.database.fields.operations import ListFieldsOperationType
from baserow.contrib.database.rows.exceptions import RowDoesNotExist
from baserow.contrib.database.rows.handler import RowHandler
from baserow.contrib.database.rows.runtime_formula_contexts import (
    HumanReadableRowContext,
)
from baserow.contrib.database.rows.signals import rows_ai_values_generation_error
from baserow.core.formula import resolve_formula
from baserow.core.formula.registries import formula_runtime_function_registry
from baserow.core.generative_ai.exceptions import ModelDoesNotBelongToType
from baserow.core.generative_ai.registries import generative_ai_model_type_registry
from baserow.core.handler import CoreHandler
from baserow.core.user.handler import User

from .models import AIField


@app.task(bind=True, queue="export")
def generate_ai_values_for_rows(self, user_id: int, field_id: int, row_ids: list[int]):
    user = User.objects.get(pk=user_id)

    ai_field = FieldHandler().get_field(
        field_id,
        base_queryset=AIField.objects.all().select_related(
            "table__database__workspace"
        ),
    )
    table = ai_field.table
    workspace = table.database.workspace

    CoreHandler().check_permissions(
        user,
        ListFieldsOperationType.type,
        workspace=workspace,
        context=table,
    )

    model = ai_field.table.get_model()
    req_row_ids = row_ids
    rows = RowHandler().get_rows(model, req_row_ids)
    if len(rows) != len(req_row_ids):
        found_rows_ids = [row.id for row in rows]
        raise RowDoesNotExist(sorted(list(set(req_row_ids) - set(found_rows_ids))))

    generative_ai_model_type = generative_ai_model_type_registry.get(
        ai_field.ai_generative_ai_type
    )
    ai_models = generative_ai_model_type.get_enabled_models(workspace=workspace)

    if ai_field.ai_generative_ai_model not in ai_models:
        raise ModelDoesNotBelongToType(model_name=ai_field.ai_generative_ai_model)

    for i, row in enumerate(rows):
        context = HumanReadableRowContext(row, exclude_field_ids=[ai_field.id])
        message = str(
            resolve_formula(
                ai_field.ai_prompt, formula_runtime_function_registry, context
            )
        )

        try:
            value = generative_ai_model_type.prompt(
                ai_field.ai_generative_ai_model, message, workspace=workspace
            )
        except Exception as exc:
            # If the prompt fails once, we should not continue with the other rows.
            rows_ai_values_generation_error.send(
                self,
                user=user,
                rows=rows[i:],
                field=ai_field,
                table=table,
                error_message=str(exc),
            )
            raise exc

        RowHandler().update_row_by_id(
            user,
            table,
            row.id,
            {ai_field.db_column: value},
            model=model,
            values_already_prepared=True,
        )
