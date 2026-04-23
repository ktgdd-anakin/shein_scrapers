category_ids = ["13086"]
category_names = ["Food-Beverages"]
max_pages = [9]

categories = [(category_names[0], category_ids[0], max_pages[0] )]



for category in categories:
    for page in range(1, category[2] + 1):
        print(f"https://us.shein.com/{category[0]}-c-{category[1]}.html?page={page}")