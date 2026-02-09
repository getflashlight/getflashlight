"""Data access layer — CRUD operations for each entity."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlmodel import Session, select

from auralake.db.models import (
    AgentState,
    AnalysisRun,
    AuditLog,
    ConsolidationGroupRecord,
    InfraCostSnapshot,
    InfraResourceMapping,
    JobProfileRecord,
    QueryPlan,
    RecommendationRecord,
)


class QueryPlanRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, plan: QueryPlan) -> QueryPlan:
        self.session.add(plan)
        self.session.commit()
        self.session.refresh(plan)
        return plan

    def get(self, plan_id: uuid.UUID) -> QueryPlan | None:
        return self.session.get(QueryPlan, plan_id)

    def list_by_workspace(self, workspace_id: str, limit: int = 100) -> list[QueryPlan]:
        stmt = select(QueryPlan).where(QueryPlan.workspace_id == workspace_id).limit(limit)
        return list(self.session.exec(stmt).all())

    def list_by_cluster(self, cluster_id: str, limit: int = 100) -> list[QueryPlan]:
        stmt = select(QueryPlan).where(QueryPlan.cluster_id == cluster_id).limit(limit)
        return list(self.session.exec(stmt).all())


class AnalysisRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, run: AnalysisRun) -> AnalysisRun:
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def get(self, run_id: uuid.UUID) -> AnalysisRun | None:
        return self.session.get(AnalysisRun, run_id)

    def list_recent(self, limit: int = 20) -> list[AnalysisRun]:
        stmt = select(AnalysisRun).order_by(AnalysisRun.started_at.desc()).limit(limit)
        return list(self.session.exec(stmt).all())

    def complete(self, run_id: uuid.UUID, summary: dict) -> None:
        run = self.session.get(AnalysisRun, run_id)
        if run:
            run.status = "completed"
            run.completed_at = datetime.utcnow()
            run.summary = summary
            self.session.commit()

    def fail(self, run_id: uuid.UUID, error: str) -> None:
        run = self.session.get(AnalysisRun, run_id)
        if run:
            run.status = "failed"
            run.completed_at = datetime.utcnow()
            run.summary = {"error": error}
            self.session.commit()


class RecommendationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, rec: RecommendationRecord) -> RecommendationRecord:
        self.session.add(rec)
        self.session.commit()
        self.session.refresh(rec)
        return rec

    def create_many(self, recs: list[RecommendationRecord]) -> list[RecommendationRecord]:
        for rec in recs:
            self.session.add(rec)
        self.session.commit()
        for rec in recs:
            self.session.refresh(rec)
        return recs

    def get(self, rec_id: uuid.UUID) -> RecommendationRecord | None:
        return self.session.get(RecommendationRecord, rec_id)

    def list_pending(self, workspace_id: str | None = None) -> list[RecommendationRecord]:
        stmt = select(RecommendationRecord).where(RecommendationRecord.status == "pending")
        if workspace_id:
            stmt = stmt.where(RecommendationRecord.workspace_id == workspace_id)
        return list(self.session.exec(stmt).all())

    def update_status(self, rec_id: uuid.UUID, status: str, pr_url: str | None = None) -> None:
        rec = self.session.get(RecommendationRecord, rec_id)
        if rec:
            rec.status = status
            if pr_url:
                rec.pr_url = pr_url
            if status == "applied":
                rec.applied_at = datetime.utcnow()
            self.session.commit()


class AuditLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, entry: AuditLog) -> AuditLog:
        self.session.add(entry)
        self.session.commit()
        self.session.refresh(entry)
        return entry

    def list_recent(self, limit: int = 50) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.executed_at.desc()).limit(limit)
        return list(self.session.exec(stmt).all())


class JobProfileRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, profile: JobProfileRecord) -> JobProfileRecord:
        existing = self.session.exec(
            select(JobProfileRecord).where(
                JobProfileRecord.workspace_id == profile.workspace_id,
                JobProfileRecord.job_id == profile.job_id,
            )
        ).first()
        if existing:
            for key, val in profile.model_dump(exclude={"id"}).items():
                setattr(existing, key, val)
            self.session.commit()
            self.session.refresh(existing)
            return existing
        self.session.add(profile)
        self.session.commit()
        self.session.refresh(profile)
        return profile

    def list_by_workspace(self, workspace_id: str) -> list[JobProfileRecord]:
        stmt = select(JobProfileRecord).where(JobProfileRecord.workspace_id == workspace_id)
        return list(self.session.exec(stmt).all())

    def get_by_job_id(self, workspace_id: str, job_id: str) -> JobProfileRecord | None:
        stmt = select(JobProfileRecord).where(
            JobProfileRecord.workspace_id == workspace_id,
            JobProfileRecord.job_id == job_id,
        )
        return self.session.exec(stmt).first()


class ConsolidationGroupRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, group: ConsolidationGroupRecord) -> ConsolidationGroupRecord:
        self.session.add(group)
        self.session.commit()
        self.session.refresh(group)
        return group

    def get(self, group_id: uuid.UUID) -> ConsolidationGroupRecord | None:
        return self.session.get(ConsolidationGroupRecord, group_id)

    def list_by_workspace(self, workspace_id: str) -> list[ConsolidationGroupRecord]:
        stmt = select(ConsolidationGroupRecord).where(
            ConsolidationGroupRecord.workspace_id == workspace_id
        )
        return list(self.session.exec(stmt).all())

    def update_status(self, group_id: uuid.UUID, status: str, pr_url: str | None = None) -> None:
        group = self.session.get(ConsolidationGroupRecord, group_id)
        if group:
            group.status = status
            if pr_url:
                group.pr_url = pr_url
            self.session.commit()


class InfraResourceMappingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, mapping: InfraResourceMapping) -> InfraResourceMapping:
        existing = self.session.exec(
            select(InfraResourceMapping).where(
                InfraResourceMapping.platform_resource_id == mapping.platform_resource_id,
                InfraResourceMapping.infra_resource_id == mapping.infra_resource_id,
            )
        ).first()
        if existing:
            existing.last_seen_at = mapping.last_seen_at
            existing.hourly_cost_usd = mapping.hourly_cost_usd
            existing.infra_resource_tags = mapping.infra_resource_tags
            self.session.commit()
            self.session.refresh(existing)
            return existing
        self.session.add(mapping)
        self.session.commit()
        self.session.refresh(mapping)
        return mapping

    def list_by_platform_resource(self, platform_resource_id: str) -> list[InfraResourceMapping]:
        stmt = select(InfraResourceMapping).where(
            InfraResourceMapping.platform_resource_id == platform_resource_id
        )
        return list(self.session.exec(stmt).all())


class InfraCostSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, snapshot: InfraCostSnapshot) -> InfraCostSnapshot:
        self.session.add(snapshot)
        self.session.commit()
        self.session.refresh(snapshot)
        return snapshot

    def create_many(self, snapshots: list[InfraCostSnapshot]) -> list[InfraCostSnapshot]:
        for s in snapshots:
            self.session.add(s)
        self.session.commit()
        for s in snapshots:
            self.session.refresh(s)
        return snapshots

    def list_by_period(
        self, start: datetime, end: datetime, service: str | None = None
    ) -> list[InfraCostSnapshot]:
        stmt = select(InfraCostSnapshot).where(
            InfraCostSnapshot.period_start >= start.date(),
            InfraCostSnapshot.period_end <= end.date(),
        )
        if service:
            stmt = stmt.where(InfraCostSnapshot.service == service)
        return list(self.session.exec(stmt).all())


class AgentStateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_or_create(self, workspace_id: str) -> AgentState:
        existing = self.session.exec(
            select(AgentState).where(AgentState.workspace_id == workspace_id)
        ).first()
        if existing:
            return existing
        state = AgentState(
            workspace_id=workspace_id, queries_collected=0, plans_collected=0, status="idle"
        )
        self.session.add(state)
        self.session.commit()
        self.session.refresh(state)
        return state

    def update(self, state: AgentState) -> AgentState:
        self.session.add(state)
        self.session.commit()
        self.session.refresh(state)
        return state
