from .contracts import CitationSourceType
from .governance_contracts import DataClassification


SOURCE_DEFINITIONS = {
    CitationSourceType.WORK_ITEM: (
        "업무",
        "현재 사용자에게 배정되거나 공개된 통합 업무 항목",
        "DWP_PLATFORM",
        DataClassification.INTERNAL,
    ),
    CitationSourceType.MAIL: (
        "메일",
        "현재 사용자의 연결된 업무 메일과 스레드 메타데이터",
        "DWP_MAIL",
        DataClassification.CONFIDENTIAL,
    ),
    CitationSourceType.CALENDAR: (
        "캘린더",
        "현재 사용자가 열람할 수 있는 일정과 회의",
        "DWP_CALENDAR",
        DataClassification.CONFIDENTIAL,
    ),
    CitationSourceType.APPROVAL_TASK: (
        "결재 업무",
        "현재 사용자에게 배정된 결재 검토 업무",
        "DWP_APPROVAL",
        DataClassification.CONFIDENTIAL,
    ),
    CitationSourceType.APPROVAL_REQUEST: (
        "결재 요청",
        "현재 사용자가 열람할 수 있는 결재 요청",
        "DWP_APPROVAL",
        DataClassification.CONFIDENTIAL,
    ),
    CitationSourceType.APPROVAL_FORM: (
        "결재 양식",
        "게시된 결재 양식과 입력 계약",
        "DWP_APPROVAL",
        DataClassification.INTERNAL,
    ),
    CitationSourceType.APPROVAL_OPERATION: (
        "결재 운영",
        "권한 범위 내 결재 운영 신호",
        "DWP_APPROVAL",
        DataClassification.RESTRICTED,
    ),
}
