import aws_cdk as core
import aws_cdk.assertions as assertions

from stackset_drift_detection.stackset_drift_detection_stack import StacksetDriftDetectionStack

# example tests. To run these tests, uncomment this file along with the example
# resource in stackset_drift_detection/stackset_drift_detection_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = StacksetDriftDetectionStack(app, "stackset-drift-detection")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
