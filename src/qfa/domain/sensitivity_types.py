from enum import StrEnum


class SensitivityType(StrEnum):
    """Categories of sensitive content that must be forwarded to the Integrity Line."""

    GENDER_INEQUALITY = "GENDER_INEQUALITY"
    GENDER_DISCRIMINATION = "GENDER_DISCRIMINATION"
    SEXUAL_VIOLENCE = "SEXUAL_VIOLENCE"
    SEXUAL_EXPLOITATION = "SEXUAL_EXPLOITATION"
    SEXUAL_ABUSE = "SEXUAL_ABUSE"
    GENDER_BASED_VIOLENCE = "GENDER_BASED_VIOLENCE"
    CORRUPTION = "CORRUPTION"
    FRAUD = "FRAUD"
    CODE_OF_CONDUCT_VIOLATION = "CODE_OF_CONDUCT_VIOLATION"

    ENVIRONMENTAL_HEALTH_AND_SAFETY_ISSUES = "ENVIRONMENTAL_HEALTH_AND_SAFETY_ISSUES"
    THREATS_AND_PHYSICAL_VIOLENCE = "THREATS_AND_PHYSICAL_VIOLENCE"
    ALCOHOL_OR_NARCOTIC_ABUSE = "ALCOHOL_OR_NARCOTIC_ABUSE"
    CONFLICT_OF_INTEREST = "CONFLICT_OF_INTEREST"
    THEFT = "THEFT"
    EMBEZZLEMENT = "EMBEZZLEMENT"
    GIFTS_BRIBES_AND_KICKBACKS = "GIFTS_BRIBES_AND_KICKBACKS"
    PROCUREMENT_OR_TENDER_VIOLATION = "PROCUREMENT_OR_TENDER_VIOLATION"
    INSIDER_TRADING = "INSIDER_TRADING"
    FALSIFICATION_OR_DESTRUCTION_OF_INFORMATION = (
        "FALSIFICATION_OR_DESTRUCTION_OF_INFORMATION"
    )
    NEPOTISM_AND_FAVOURITISM = "NEPOTISM_AND_FAVOURITISM"
    ABUSE_OF_OFFICIAL_POSITION = "ABUSE_OF_OFFICIAL_POSITION"
    UNFAIR_TREATMENT_OF_EMPLOYEES = "UNFAIR_TREATMENT_OF_EMPLOYEES"
    UNFAIR_DISMISSAL = "UNFAIR_DISMISSAL"
    HARASSMENT = "HARASSMENT"
    PRIVACY_AND_CONFIDENTIALITY_ISSUES = "PRIVACY_AND_CONFIDENTIALITY_ISSUES"
    WORKPLACE_BULLYING = "WORKPLACE_BULLYING"
    SEXUAL_HARASSMENT = "SEXUAL_HARASSMENT"
    VANDALISM = "VANDALISM"
    GIFTS_AND_HOSPITALITY = "GIFTS_AND_HOSPITALITY"
    ENVIRONMENTAL_VIOLATIONS = "ENVIRONMENTAL_VIOLATIONS"
    FOOD_SAFETY = "FOOD_SAFETY"
    RETALIATION_AGAINST_WHISTLEBLOWERS = "RETALIATION_AGAINST_WHISTLEBLOWERS"
    INAPPROPRIATE_BEHAVIOUR = "INAPPROPRIATE_BEHAVIOUR"


SENSITIVITY_TYPE_DESCRIPTIONS: dict[SensitivityType, str] = {
    SensitivityType.GENDER_INEQUALITY: "Apply when feedback indicates unequal opportunities or treatment due to gender.",
    SensitivityType.GENDER_DISCRIMINATION: "Apply when feedback describes exclusion, restriction, or unfair treatment based on sex.",
    SensitivityType.SEXUAL_VIOLENCE: "Apply when feedback reports sexual assault, coercion, or other sexual acts of violence.",
    SensitivityType.SEXUAL_EXPLOITATION: "Apply when feedback indicates abuse of power, trust, or vulnerability for sexual gain.",
    SensitivityType.SEXUAL_ABUSE: "Apply when feedback reports actual or threatened sexual intrusion under force or coercion.",
    SensitivityType.GENDER_BASED_VIOLENCE: "Apply when feedback describes violence driven by gender roles or gender-based power imbalances.",
    SensitivityType.CORRUPTION: "Apply when feedback alleges bribery, abuse of authority, embezzlement, or other corrupt conduct.",
    SensitivityType.FRAUD: "Apply when feedback reports deception used for unlawful financial or material gain.",
    SensitivityType.CODE_OF_CONDUCT_VIOLATION: "Apply when feedback indicates behavior that violates the organizational code of conduct.",
    SensitivityType.ENVIRONMENTAL_HEALTH_AND_SAFETY_ISSUES: "Apply when feedback reports non-compliance with environmental, health, or safety standards.",
    SensitivityType.THREATS_AND_PHYSICAL_VIOLENCE: "Apply when feedback includes threats, intimidation, or acts of physical violence.",
    SensitivityType.ALCOHOL_OR_NARCOTIC_ABUSE: "Apply when feedback reports abuse of alcohol or narcotic substances.",
    SensitivityType.CONFLICT_OF_INTEREST: "Apply when feedback indicates personal interests that may compromise objective decision-making.",
    SensitivityType.THEFT: "Apply when feedback reports unauthorized taking or appropriation of property or assets.",
    SensitivityType.EMBEZZLEMENT: "Apply when feedback reports improper or unauthorized use of funds, systems, or resources.",
    SensitivityType.GIFTS_BRIBES_AND_KICKBACKS: "Apply when feedback describes improper gifts, bribes, kickbacks, or undue advantages.",
    SensitivityType.PROCUREMENT_OR_TENDER_VIOLATION: "Apply when feedback reports manipulation or abuse of procurement or tender procedures.",
    SensitivityType.INSIDER_TRADING: "Apply when feedback describes trading based on confidential internal information.",
    SensitivityType.FALSIFICATION_OR_DESTRUCTION_OF_INFORMATION: "Apply when feedback reports concealment, falsification, or destruction of important information.",
    SensitivityType.NEPOTISM_AND_FAVOURITISM: "Apply when feedback indicates unfair advantage based on family or personal relationships.",
    SensitivityType.ABUSE_OF_OFFICIAL_POSITION: "Apply when feedback reports misuse of position, authority, or organizational resources for personal benefit.",
    SensitivityType.UNFAIR_TREATMENT_OF_EMPLOYEES: "Apply when feedback describes unjust employee treatment, decisions, or disciplinary actions.",
    SensitivityType.UNFAIR_DISMISSAL: "Apply when feedback reports termination without valid, lawful, or justified cause.",
    SensitivityType.HARASSMENT: "Apply when feedback describes offensive, hostile, intimidating, or unwanted conduct.",
    SensitivityType.PRIVACY_AND_CONFIDENTIALITY_ISSUES: "Apply when feedback reports unauthorized disclosure or misuse of personal or confidential data.",
    SensitivityType.WORKPLACE_BULLYING: "Apply when feedback reports repeated humiliating, hostile, or frightening behavior at work.",
    SensitivityType.SEXUAL_HARASSMENT: "Apply when feedback describes unwanted sexual advances, contact, or sexualized behavior.",
    SensitivityType.VANDALISM: "Apply when feedback reports intentional damage or destruction of property.",
    SensitivityType.GIFTS_AND_HOSPITALITY: "Apply when feedback describes gifts or hospitality that compromise ethics, fairness, or objectivity.",
    SensitivityType.ENVIRONMENTAL_VIOLATIONS: "Apply when feedback reports pollution, resource misuse, or other environmentally harmful activities.",
    SensitivityType.FOOD_SAFETY: "Apply when feedback reports unsafe food handling, hygiene, or protocol violations.",
    SensitivityType.RETALIATION_AGAINST_WHISTLEBLOWERS: "Apply when feedback reports punishment, intimidation, or persecution after misconduct reporting.",
    SensitivityType.INAPPROPRIATE_BEHAVIOUR: "Apply when feedback describes workplace conduct that is inappropriate even if not harassment.",
}
