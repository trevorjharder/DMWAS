web: flask --app app db upgrade && exec gunicorn --workers 2 --threads 4 --bind 0.0.0.0:$PORT 'app:create_app()'
