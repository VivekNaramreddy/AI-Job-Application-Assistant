"""Local search-field suggestions; replaceable with online catalogs later."""

from typing import Dict, List


JOB_TITLES = [
    "Account Executive", "Account Manager", "AI Engineer", "Backend Developer",
    "Brand Manager", "Business Analyst", "Business Development Manager",
    "Cloud Engineer", "Communications Manager", "Content Creator",
    "Content Marketing Manager", "Content Strategist", "Copywriter",
    "Customer Success Manager", "Data Analyst", "Data Engineer", "Data Scientist",
    "DevOps Engineer", "Digital Marketing Analyst", "Digital Marketing Manager",
    "E-commerce Manager", "Email Marketing Specialist", "Financial Analyst",
    "Frontend Developer", "Full Stack Developer", "Growth Marketing Manager",
    "Human Resources Manager", "IT Support Specialist", "Marketing Analyst",
    "Marketing Assistant", "Marketing Coordinator", "Marketing Director",
    "Marketing Executive", "Marketing Manager", "Marketing Operations Manager",
    "Marketing Specialist", "Media Planner", "Operations Analyst",
    "Operations Manager", "Paid Media Specialist", "Performance Marketing Manager",
    "Product Designer", "Product Manager", "Product Marketing Manager",
    "Project Coordinator", "Project Manager", "Public Relations Specialist",
    "Quality Assurance Engineer", "Recruiter", "Sales Development Representative",
    "Sales Manager", "SEO Analyst", "SEO Manager", "SEO Specialist",
    "Social Media Manager", "Software Engineer", "Systems Administrator",
    "Technical Writer", "UX Designer", "UX Researcher", "Web Developer",
]

KEYWORDS = [
    "A/B testing", "Adobe Analytics", "Agile", "AWS", "B2B", "B2C",
    "Brand strategy", "Campaign management", "Canva", "Cloud computing",
    "Content marketing", "Content strategy", "Copywriting", "CRM", "CSS",
    "Data analysis", "Digital marketing", "Docker", "Email marketing", "Excel",
    "Figma", "GA4", "Git", "Google Ads", "Google Analytics", "Google Cloud",
    "Graphic design", "Growth marketing", "HubSpot", "HTML", "Java",
    "JavaScript", "Jira", "Kubernetes", "Lead generation", "Looker",
    "Marketing automation", "Market research", "Meta Ads", "Microsoft Office",
    "Node.js", "Paid search", "Paid social", "Performance marketing", "Power BI",
    "Product marketing", "Project management", "Python", "React", "Salesforce",
    "SEM", "SEO", "Shopify", "Social media", "SQL", "Tableau", "Team leadership",
    "TypeScript", "User research", "WordPress", "Writing",
]

AVOID_KEYWORDS = [
    "Commission only", "Door-to-door", "Independent contractor", "On call",
    "Security clearance required", "Unpaid", "Volunteer", "Weekend required",
]

LOCATIONS = [
    "United States", "Remote", "New York, NY", "Los Angeles, CA", "Chicago, IL",
    "Houston, TX", "Phoenix, AZ", "Philadelphia, PA", "San Antonio, TX",
    "San Diego, CA", "Dallas, TX", "Austin, TX", "San Jose, CA",
    "San Francisco, CA", "Seattle, WA", "Denver, CO", "Boston, MA",
    "Atlanta, GA", "Miami, FL", "Washington, DC", "Raleigh, NC",
    "Charlotte, NC", "Portland, OR", "Minneapolis, MN", "Nashville, TN",
]

CATALOGS: Dict[str, List[str]] = {
    "target_roles": JOB_TITLES,
    "target_locations": LOCATIONS,
    "required_keywords": KEYWORDS,
    "preferred_keywords": KEYWORDS,
    "excluded_keywords": AVOID_KEYWORDS + KEYWORDS,
}


def find_suggestions(field: str, query: str, limit: int = 10) -> List[str]:
    """Return prefix-first, case-insensitive suggestions for a guided field."""
    catalog = CATALOGS.get(field)
    if catalog is None:
        raise KeyError(field)
    normalized = " ".join(query.lower().split())
    if not normalized:
        return catalog[:limit]
    matches = [value for value in catalog if normalized in value.lower()]
    matches.sort(key=lambda value: (not value.lower().startswith(normalized), value.lower()))
    return matches[:limit]
