from enum import StrEnum


class SensitivityType(StrEnum):
    """Categories of sensitive content that must be forwarded to the Integrity Line."""

    GENDER_INEQUALITY = "Apply when feedback indicates unequal opportunities or treatment due to gender."
    GENDER_DISCRIMINATION = "Apply when feedback describes exclusion, restriction, or unfair treatment based on sex."
    SEXUAL_VIOLENCE = "Apply when feedback reports sexual assault, coercion, or other sexual acts of violence."
    SEXUAL_EXPLOITATION = "Apply when feedback indicates abuse of power, trust, or vulnerability for sexual gain."
    SEXUAL_ABUSE = "Apply when feedback reports actual or threatened sexual intrusion under force or coercion."
    GENDER_BASED_VIOLENCE = "Apply when feedback describes violence driven by gender roles or gender-based power imbalances."
    CORRUPTION = "Apply when feedback alleges bribery, abuse of authority, embezzlement, or other corrupt conduct."
    FRAUD = "Apply when feedback reports deception used for unlawful financial or material gain."
    CODE_OF_CONDUCT_VIOLATION = "Apply when feedback indicates behavior that violates the organizational code of conduct."

    ENVIRONMENTAL_HEALTH_AND_SAFETY_ISSUES = "Apply when feedback reports non-compliance with environmental, health, or safety standards."
    THREATS_AND_PHYSICAL_VIOLENCE = "Apply when feedback includes threats, intimidation, or acts of physical violence."
    ALCOHOL_OR_NARCOTIC_ABUSE = (
        "Apply when feedback reports abuse of alcohol or narcotic substances."
    )
    CONFLICT_OF_INTEREST = "Apply when feedback indicates personal interests that may compromise objective decision-making."
    THEFT = "Apply when feedback reports unauthorized taking or appropriation of property or assets."
    EMBEZZLEMENT = "Apply when feedback reports improper or unauthorized use of funds, systems, or resources."
    GIFTS_BRIBES_AND_KICKBACKS = "Apply when feedback describes improper gifts, bribes, kickbacks, or undue advantages."
    PROCUREMENT_OR_TENDER_VIOLATION = "Apply when feedback reports manipulation or abuse of procurement or tender procedures."
    INSIDER_TRADING = "Apply when feedback describes trading based on confidential internal information."
    FALSIFICATION_OR_DESTRUCTION_OF_INFORMATION = "Apply when feedback reports concealment, falsification, or destruction of important information."
    NEPOTISM_AND_FAVOURITISM = "Apply when feedback indicates unfair advantage based on family or personal relationships."
    ABUSE_OF_OFFICIAL_POSITION = "Apply when feedback reports misuse of position, authority, or organizational resources for personal benefit."
    UNFAIR_TREATMENT_OF_EMPLOYEES = "Apply when feedback describes unjust employee treatment, decisions, or disciplinary actions."
    UNFAIR_DISMISSAL = "Apply when feedback reports termination without valid, lawful, or justified cause."
    HARASSMENT = "Apply when feedback describes offensive, hostile, intimidating, or unwanted conduct."
    PRIVACY_AND_CONFIDENTIALITY_ISSUES = "Apply when feedback reports unauthorized disclosure or misuse of personal or confidential data."
    WORKPLACE_BULLYING = "Apply when feedback reports repeated humiliating, hostile, or frightening behavior at work."
    SEXUAL_HARASSMENT = "Apply when feedback describes unwanted sexual advances, contact, or sexualized behavior."
    VANDALISM = (
        "Apply when feedback reports intentional damage or destruction of property."
    )
    GIFTS_AND_HOSPITALITY = "Apply when feedback describes gifts or hospitality that compromise ethics, fairness, or objectivity."
    ENVIRONMENTAL_VIOLATIONS = "Apply when feedback reports pollution, resource misuse, or other environmentally harmful activities."
    FOOD_SAFETY = "Apply when feedback reports unsafe food handling, hygiene, or protocol violations."
    RETALIATION_AGAINST_WHISTLEBLOWERS = "Apply when feedback reports punishment, intimidation, or persecution after misconduct reporting."
    INAPPROPRIATE_BEHAVIOUR = "Apply when feedback describes workplace conduct that is inappropriate even if not harassment."
