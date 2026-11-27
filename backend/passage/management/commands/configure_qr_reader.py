import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


QR_FUNCTION = """
CREATE OR REPLACE FUNCTION public.qr_active_student_badge(p_user_id bigint)
RETURNS TABLE (
    user_id bigint,
    identifier varchar(150),
    user_role varchar(12),
    created_at timestamp with time zone,
    student_name varchar(120),
    class_name varchar(80),
    school varchar(120),
    student_active boolean,
    badge_id uuid,
    badge_secret text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT u.id, u.username, u.role, u.date_joined,
           s.name, s.class_name, s.school, s.active,
           b.badge_id, b.secret_ciphertext
      FROM public.passage_user AS u
      JOIN public.passage_student AS s ON s.account_id = u.id
      JOIN public.passage_badge AS b ON b.student_id = s.id
     WHERE u.id = p_user_id
       AND u.role = 'student'
       AND u.is_active
       AND s.active
       AND b.state = 'active'
     ORDER BY b.issued_at DESC
     LIMIT 1
$$
"""


class Command(BaseCommand):
    help = 'Configure un compte PostgreSQL QR en lecture restreinte.'

    def handle(self, *args, **options):
        role = os.getenv('QR_DB_USER', '')
        password = os.getenv('QR_DB_PASSWORD', '')
        if not role or not password or len(password) < 32:
            raise CommandError('QR_DB_USER et un QR_DB_PASSWORD de 32 caractères minimum sont requis.')

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT format('CREATE ROLE %%I LOGIN PASSWORD %%L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS', %s, %s) "
                "WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                [role, password, role],
            )
            create_statement = cursor.fetchone()
            if create_statement:
                cursor.execute(create_statement[0])
            else:
                cursor.execute(
                    "SELECT format('ALTER ROLE %%I LOGIN PASSWORD %%L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS', %s, %s)",
                    [role, password],
                )
                cursor.execute(cursor.fetchone()[0])

            cursor.execute(
                "SELECT format('GRANT CONNECT ON DATABASE %%I TO %%I', %s, %s)",
                [connection.settings_dict['NAME'], role],
            )
            cursor.execute(cursor.fetchone()[0])
            cursor.execute(
                "SELECT format('GRANT USAGE ON SCHEMA public TO %%I', %s)",
                [role],
            )
            cursor.execute(cursor.fetchone()[0])
            cursor.execute(QR_FUNCTION)
            cursor.execute('REVOKE ALL ON FUNCTION public.qr_active_student_badge(bigint) FROM PUBLIC')
            cursor.execute(
                "SELECT format('GRANT EXECUTE ON FUNCTION public.qr_active_student_badge(bigint) TO %%I', %s)",
                [role],
            )
            cursor.execute(cursor.fetchone()[0])
            cursor.execute(
                "SELECT format('ALTER ROLE %%I SET default_transaction_read_only = on', %s)",
                [role],
            )
            cursor.execute(cursor.fetchone()[0])

        self.stdout.write(self.style.SUCCESS('Accès PostgreSQL du générateur QR configuré en lecture restreinte.'))
