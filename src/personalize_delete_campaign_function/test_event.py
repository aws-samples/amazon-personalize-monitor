import boto3
import json

event_bridge = boto3.client(service_name = 'events', region_name = 'us-east-1')

campaign_arn = 'aws:arn:personalize:/campaign/yada'
reason = 'Something really bad happend but I fixed it for you!'

print('Sending event')

event_bridge.put_events(
    Entries=[
        {
            'Source': 'personalize.monitor',
            'Resources': [ campaign_arn ],
            'DetailType': 'PersonalizeCampaignDeleted',
            'Detail': json.dumps({
                'ARN': campaign_arn,
                'Reason': reason
            })
        }
    ]
)

print('Event sent')