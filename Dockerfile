FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
LABEL org.opencontainers.image.title="kevenos" org.opencontainers.image.version="1.0.0"
WORKDIR /app
COPY agent-requirements.txt ./
RUN pip install --no-cache-dir -r agent-requirements.txt
COPY event_operations.py event_transition.py admin_agenda.py database_guard.py event_roles.py event_push.py event_quiz.py audience_questions.py quiz-bank.json agent_api.py agent_mcp.py agent_client.py event_commands.py event_schedule.py event_assistant.py rehearsal_reconcile.py server.py participant_companion.py participant_workspace.py registration_desk.py final_event.py event_config.py event_access.py event_profiles.py event_mail.py event_workspace.py event-guide.json usage-guides.json content.json ./
COPY static ./static
COPY docs ./docs
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data && chown app:app /data
ENV KEVENOS_DATA=/data KEVENOS_PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)" || exit 1
USER app
ENTRYPOINT ["python", "database_guard.py"]
