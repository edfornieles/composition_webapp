from flask import Flask
from djangoscrap.celery_app import long_running_task

app = Flask(__name__)

print("Test Hello")
data = "Sample data"  # Define a valid input
task = long_running_task.delay(data)  # Run task in background
print("Test Hello2")
#task = long_running_task.delay(data)  # Runs in background again
#print(jsonify({"task_id": task.id, "status": "Task started"}), 202)

if __name__ == '__main__':
    app.run(debug=True)
