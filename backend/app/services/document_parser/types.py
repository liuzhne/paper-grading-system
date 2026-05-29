from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Optional


@dataclass
class ParsedParagraph:
    paragraph_id: str
    page: int
    text: str


@dataclass
class ParsedSection:
    section_id: str
    title: str
    level: int
    page_start: int
    page_end: int
    paragraphs: list[ParsedParagraph] = field(default_factory=list)


@dataclass
class StructureCheck:
    code: str
    name: str
    passed: bool
    message: str
    location: Optional[str] = None


@dataclass
class ParsedPaper:
    title: Optional[str]
    student_id: Optional[str]
    student_name: Optional[str]
    institution: Optional[str]
    department: Optional[str]
    major: Optional[str]
    advisor: Optional[str]
    sections: list[ParsedSection]
    references: list[str]
    full_text: str
    structure_checks: list[StructureCheck]
    parse_quality: float

    def to_dict(self):
        return asdict(self)
