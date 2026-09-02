from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Market Monitor migrations require a writer-owned connection")

context.configure(
    connection=connection,
    target_metadata=None,
    transactional_ddl=True,
    transaction_per_migration=True,
)

with context.begin_transaction():
    context.run_migrations()
