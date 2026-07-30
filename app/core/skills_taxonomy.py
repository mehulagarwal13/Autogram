"""
Canonical skill names mapped from common aliases/variants.
Not exhaustive — expand this as you see real resumes hit edge cases.
"""

SKILL_ALIASES: dict[str, str] = {
    # ======================
    # Programming Languages
    # ======================
    "python": "Python",
    "python3": "Python",
    "py": "Python",

    "java": "Java",

    "javascript": "JavaScript",
    "javascript (es6)": "JavaScript",
    "javascript es6": "JavaScript",
    "js": "JavaScript",
    "es6": "JavaScript",

    "typescript": "TypeScript",
    "ts": "TypeScript",

    "c": "C",
    "c language": "C",

    "c++": "C++",
    "cpp": "C++",

    "c#": "C#",
    "csharp": "C#",

    "go": "Go",
    "golang": "Go",

    "rust": "Rust",

    "ruby": "Ruby",

    "php": "PHP",

    "swift": "Swift",

    "kotlin": "Kotlin",

    "scala": "Scala",

    "r": "R",

    "perl": "Perl",

    "dart": "Dart",

    "matlab": "MATLAB",

    "objective-c": "Objective-C",
    "objective c": "Objective-C",

    "bash": "Bash",
    "shell": "Shell Scripting",
    "shell scripting": "Shell Scripting",

    "powershell": "PowerShell",

    # ======================
    # Frontend
    # ======================
    "html": "HTML",
    "html5": "HTML",

    "css": "CSS",
    "css3": "CSS",

    "sass": "Sass",
    "scss": "Sass",

    "bootstrap": "Bootstrap",

    "tailwind": "Tailwind CSS",
    "tailwindcss": "Tailwind CSS",

    "material ui": "Material UI",
    "mui": "Material UI",

    "chakra": "Chakra UI",
    "chakra ui": "Chakra UI",

    "react": "React",
    "reactjs": "React",
    "react.js": "React",

    "next": "Next.js",
    "nextjs": "Next.js",
    "next.js": "Next.js",

    "vue": "Vue.js",
    "vuejs": "Vue.js",
    "vue.js": "Vue.js",

    "nuxt": "Nuxt.js",
    "nuxtjs": "Nuxt.js",

    "angular": "Angular",
    "angularjs": "AngularJS",

    "svelte": "Svelte",

    "redux": "Redux",

    "jquery": "jQuery",

    # ======================
    # Backend
    # ======================
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",

    "express": "Express.js",
    "expressjs": "Express.js",
    "express.js": "Express.js",

    "nestjs": "NestJS",
    "nest": "NestJS",

    "django": "Django",

    "flask": "Flask",

    "fastapi": "FastAPI",

    "spring": "Spring",
    "spring boot": "Spring Boot",
    "springboot": "Spring Boot",

    "laravel": "Laravel",

    "asp.net": "ASP.NET",
    ".net": ".NET",
    "dotnet": ".NET",

    "ruby on rails": "Ruby on Rails",
    "rails": "Ruby on Rails",

    # ======================
    # Databases
    # ======================
    "sql": "SQL",

    "mysql": "MySQL",

    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",

    "sqlite": "SQLite",

    "oracle": "Oracle Database",

    "sql server": "SQL Server",
    "mssql": "SQL Server",

    "mongodb": "MongoDB",
    "mongo": "MongoDB",

    "redis": "Redis",

    "cassandra": "Apache Cassandra",

    "dynamodb": "DynamoDB",

    "firebase": "Firebase",

    "supabase": "Supabase",

    "elasticsearch": "Elasticsearch",
    "elastic search": "Elasticsearch",

    # ======================
    # Cloud
    # ======================
    "aws": "AWS",
    "amazon web services": "AWS",

    "azure": "Microsoft Azure",

    "gcp": "Google Cloud Platform",
    "google cloud": "Google Cloud Platform",
    "google cloud platform": "Google Cloud Platform",

    "digitalocean": "DigitalOcean",

    "heroku": "Heroku",

    "vercel": "Vercel",

    "netlify": "Netlify",

    # ======================
    # DevOps
    # ======================
    "docker": "Docker",

    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",

    "jenkins": "Jenkins",

    "github actions": "GitHub Actions",

    "gitlab ci": "GitLab CI/CD",
    "gitlab ci/cd": "GitLab CI/CD",

    "terraform": "Terraform",

    "ansible": "Ansible",

    "helm": "Helm",

    "nginx": "Nginx",

    "apache": "Apache HTTP Server",

    # ======================
    # Version Control
    # ======================
    "git": "Git",

    "github": "GitHub",

    "gitlab": "GitLab",

    "bitbucket": "Bitbucket",

    # ======================
    # APIs
    # ======================
    "rest": "REST APIs",
    "rest api": "REST APIs",
    "rest apis": "REST APIs",
    "restful api": "REST APIs",
    "restful apis": "REST APIs",

    "graphql": "GraphQL",

    "grpc": "gRPC",

    "soap": "SOAP",

    # ======================
    # AI / ML
    # ======================
    "ml": "Machine Learning",
    "machine learning": "Machine Learning",

    "deep learning": "Deep Learning",
    "dl": "Deep Learning",

    "artificial intelligence": "Artificial Intelligence",
    "ai": "Artificial Intelligence",

    "nlp": "Natural Language Processing",
    "natural language processing": "Natural Language Processing",

    "computer vision": "Computer Vision",
    "cv": "Computer Vision",

    "llm": "LLM",
    "llms": "LLM",
    "large language model": "LLM",
    "large language models": "LLM",

    "rag": "Retrieval-Augmented Generation",

    "langchain": "LangChain",

    "llamaindex": "LlamaIndex",

    "huggingface": "Hugging Face",
    "hugging face": "Hugging Face",

    "transformers": "Transformers",

    "openai": "OpenAI API",

    "gemini": "Gemini API",

    "claude": "Claude API",

    "crewai": "CrewAI",

    "autogen": "AutoGen",

    # ======================
    # ML Frameworks
    # ======================
    "tensorflow": "TensorFlow",

    "keras": "Keras",

    "pytorch": "PyTorch",

    "scikit-learn": "Scikit-learn",
    "sklearn": "Scikit-learn",

    "xgboost": "XGBoost",

    "lightgbm": "LightGBM",

    "catboost": "CatBoost",

    # ======================
    # Data Science
    # ======================
    "numpy": "NumPy",

    "pandas": "Pandas",

    "matplotlib": "Matplotlib",

    "seaborn": "Seaborn",

    "plotly": "Plotly",

    "scipy": "SciPy",

    "jupyter": "Jupyter Notebook",
    "jupyter notebook": "Jupyter Notebook",

    # ======================
    # Big Data
    # ======================
    "spark": "Apache Spark",

    "apache spark": "Apache Spark",

    "hadoop": "Apache Hadoop",

    "kafka": "Apache Kafka",

    "airflow": "Apache Airflow",

    "databricks": "Databricks",

    # ======================
    # Testing
    # ======================
    "pytest": "PyTest",

    "junit": "JUnit",

    "selenium": "Selenium",

    "playwright": "Playwright",

    "cypress": "Cypress",

    "jest": "Jest",

    "mocha": "Mocha",

    # ======================
    # Mobile
    # ======================
    "android": "Android",

    "ios": "iOS",

    "flutter": "Flutter",

    "react native": "React Native",

    "xamarin": "Xamarin",

    # ======================
    # Messaging
    # ======================
    "rabbitmq": "RabbitMQ",

    "kafka": "Apache Kafka",

    # ======================
    # Operating Systems
    # ======================
    "linux": "Linux",

    "ubuntu": "Ubuntu",

    "windows": "Windows",

    "macos": "macOS",

    # ======================
    # Miscellaneous
    # ======================
    "oauth": "OAuth",
    "jwt": "JWT",
    "json": "JSON",
    "xml": "XML",
    "yaml": "YAML",
    "ci/cd": "CI/CD",
    "microservices": "Microservices",
    "agile": "Agile",
    "scrum": "Scrum",
}


def canonicalize_skill(raw_skill: str) -> str:
    key = raw_skill.strip().lower()
    return SKILL_ALIASES.get(key, raw_skill.strip())