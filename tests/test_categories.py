import pytest

from outsource.categories import classify_role


@pytest.mark.parametrize(("title", "category"), [
    ("Software Engineering Intern", "swe"),
    ("Backend Developer", "swe"),
    ("Front-End Engineer", "swe"),
    ("Full Stack Intern", "swe"),
    ("Mobile Engineer", "swe"),
    ("Systems Engineer", "swe"),
    ("Systems Software Engineering Internship", "swe"),
    ("Embedded Software Engineer", "swe"),
    ("Firmware Engineer", "swe"),
    ("Platform Engineering Intern", "swe"),
    ("Site Reliability Engineer", "swe"),
    ("SRE Intern", "swe"),
    ("DevOps Engineer", "swe"),
    ("Test Automation Engineer", "swe"),
    ("Security Software Engineer", "swe"),
    ("Robotics Software Engineer", "swe"),
    ("Technology Developer Summer Internship", "swe"),
    ("Intern Developer – Adobe 2027", "swe"),
    ("Data Engineer", "data_ai"),
    ("Data Science Intern", "data_ai"),
    ("Machine Learning Software Engineer", "data_ai"),
    ("AI Engineer", "data_ai"),
    ("Applied Scientist", "data_ai"),
    ("Product Manager Intern", "pm"),
    ("Technical Program Management Intern", "pm"),
    ("Technical Project Manager", "pm"),
    ("IT Support Intern", "it_security"),
    ("Systems Administrator", "it_security"),
    ("Cloud Administration Intern", "it_security"),
    ("Cybersecurity Analyst", "it_security"),
    ("Security Engineer", "it_security"),
    ("Network Engineer", "it_security"),
    ("Quantitative Developer", "quant"),
    ("Quant Research Intern", "quant"),
    ("Quantitative Trading Analyst", "quant"),
    ("Data Analyst", "analyst"),
    ("Business Intelligence Intern", "analyst"),
    ("Financial Analyst", "analyst"),
    ("Product Analyst", "analyst"),
    ("Software Business Analyst", "analyst"),
    ("Electrical Engineer", "hardware"),
    ("FPGA Design Engineer", "hardware"),
    ("Robotics Engineer", "hardware"),
    ("UX Research Intern", "design"),
    ("UI Designer", "design"),
    ("Product Design Intern", "design"),
    ("Intern", "other"),
    ("Sales Associate", "other"),
    ("Retail Intern", "other"),
    ("Chair Design Intern", "other"),
])
def test_role_families(title, category):
    assert classify_role(title).key == category


@pytest.mark.parametrize(("title", "summary", "category"), [
    ("Systems Engineer", "Maintain enterprise network administration.", "it_security"),
    ("Systems Engineer", "Design physical systems and electrical hardware.", "hardware"),
    ("Intern", "You will work on backend development.", "swe"),
    ("Intern", "<p>Join our data science team.</p>", "data_ai"),
    ("Intern", "It is exciting: try it with our backend team.", "swe"),
    ("Intern", "Collaborate across software and product management teams.", "other"),
    ("Software Engineer", "We are a quantitative trading company using AI.", "swe"),
    ("Data Analyst", "Collaborate with software engineers and data scientists.", "analyst"),
    ("Intern", "", "other"),
])
def test_description_fallback_and_title_precedence(title, summary, category):
    assert classify_role(title, summary).key == category
