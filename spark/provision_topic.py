"""Create an explicitly named ordered-demo topic, without changing existing topics."""
import argparse
import os

def main():
    from kafka.admin import KafkaAdminClient, NewTopic
    from kafka.errors import TopicAlreadyExistsError
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP", "localhost:29092"))
    parser.add_argument("--topic", default="telecom.events.ordered.v2")
    parser.add_argument("--partitions", type=int, default=1)
    args = parser.parse_args()
    if args.partitions < 1:
        parser.error("partitions must be positive")
    admin = KafkaAdminClient(bootstrap_servers=args.bootstrap)
    try:
        try:
            admin.create_topics([NewTopic(args.topic, num_partitions=args.partitions, replication_factor=1)])
        except TopicAlreadyExistsError:
            pass
        description = admin.describe_topics([args.topic])[0]
        actual = len(description["partitions"])
        if actual != args.partitions:
            raise SystemExit(f"Existing topic has {actual} partitions; requested {args.partitions}. Use a new topic.")
        print(f"topic={args.topic} partitions={actual}; existing configuration was not changed")
    finally:
        admin.close()

if __name__ == "__main__":
    main()
