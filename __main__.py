import pulumi
import pulumi_eks as eks
import pulumi_aws as aws
import pulumi_kubernetes as k8s
import json

cluster_name = "eks-demo"
cluster_tag = f"kubernetes.io/cluster/{cluster_name}"

public_subnet_cidrs = ["172.32.0.0/20", "172.32.16.0/20"]

# Use 2 AZs for our cluster
avail_zones = ["ap-northeast-2a", "ap-northeast-2c"]

# Create VPC for EKS Cluster
vpc = aws.ec2.Vpc(
	"eks-vpc",
	cidr_block="172.32.0.0/16",
    tags={
        'Name': 'eks-vpc',
    }
)

igw = aws.ec2.InternetGateway(
	"eks-igw",
	vpc_id=vpc.id,
    tags={
        'Name': 'eks-igw',
    }
)

route_table = aws.ec2.RouteTable(
	"eks-route-table",
	vpc_id=vpc.id,
	routes=[
		{
			"cidr_block": "0.0.0.0/0",
			"gateway_id": igw.id
		}
	],
    tags={
        'Name': 'eks-rt',
    }
)

public_subnet_ids = []

# Create public subnets that will be used for the AWS Load Balancer Controller
"""
Public Subnets should be resource tagged with: kubernetes.io/role/elb: 1
Private Subnets should be tagged with: kubernetes.io/role/internal-elb: 1
Both private and public subnets should be tagged with: kubernetes.io/cluster/${your-cluster-name}: owned
https://stackoverflow.com/questions/66039501/eks-alb-is-not-to-able-to-auto-discover-subnets
"""            
for zone, public_subnet_cidr  in zip(avail_zones, public_subnet_cidrs):
    public_subnet = aws.ec2.Subnet(
        f"eks-public-subnet-{zone}",
        assign_ipv6_address_on_creation=False,
        vpc_id=vpc.id,
        map_public_ip_on_launch=True,
        cidr_block=public_subnet_cidr,
        availability_zone=zone,
        tags={
	     	# Custom tags for subnets
            "Name": f"eks-public-subnet-{zone}",
            cluster_tag: "owned",
            "kubernetes.io/role/elb": "1",
            f"kubernetes.io/cluster/{cluster_name}": "owned",
        }
    )

    aws.ec2.RouteTableAssociation(
        f"eks-public-rta-{zone}",
        route_table_id=route_table.id,
        subnet_id=public_subnet.id,
    )
    public_subnet_ids.append(public_subnet.id)

# Create an EKS cluster.
cluster = eks.Cluster(
    cluster_name,
	name=cluster_name,
    vpc_id=vpc.id,
    instance_type="t2.medium",
    desired_capacity=2,
    min_size=1,
    max_size=2,
    provider_credential_opts={'profile_name': 'user1'},
    public_subnet_ids=public_subnet_ids,
    create_oidc_provider=True,
)

# Export the cluster's kubeconfig.
pulumi.export("kubeconfig", cluster.kubeconfig)
# pulumi stack output kubeconfig > ../.kube/config  (to your directory)


"""
Add IAM Policy. 
This policy needs to be attached to the EKS worker nodes 
so that these nodes have the proper permissions to communicate 
with the Amazon Load Balancers.
"""

aws_lb_ns = "aws-lb-controller"
service_account_name = f"system:serviceaccount:{aws_lb_ns}:aws-lb-controller-serviceaccount"
oidc_arn = cluster.core.oidc_provider.arn
oidc_url = cluster.core.oidc_provider.url

# Create IAM role for AWS LB controller service account
iam_role = aws.iam.Role(
    "aws-loadbalancer-controller-role",
    assume_role_policy=pulumi.Output.all(oidc_arn, oidc_url).apply(
        lambda args: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Federated": args[0],
                        },
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Condition": {
                            "StringEquals": {f"{args[1]}:sub": service_account_name},
                        },
                    }
                ],
            }
        )
    ),
)

with open("files/iam_policy.json") as policy_file:
    policy_doc = policy_file.read()

iam_policy = aws.iam.Policy(
    "aws-loadbalancer-controller-policy",
    policy=policy_doc,
    opts=pulumi.ResourceOptions(parent=iam_role),
)

# Attach IAM Policy to IAM Role
aws.iam.PolicyAttachment(
    "aws-loadbalancer-controller-attachment",
    policy_arn=iam_policy.arn,
    roles=[iam_role.name],
    opts=pulumi.ResourceOptions(parent=iam_role),
)

provider = k8s.Provider("provider", kubeconfig=cluster.kubeconfig)

namespace = k8s.core.v1.Namespace(
    f"{aws_lb_ns}-ns",
    metadata={
        "name": aws_lb_ns,
        "labels": {
            "app.kubernetes.io/name": "aws-load-balancer-controller",
        }
    },
    opts=pulumi.ResourceOptions(
        provider=provider,
        parent=provider,
    )
)

service_account = k8s.core.v1.ServiceAccount(
    "aws-lb-controller-sa",
    metadata={
        "name": "aws-lb-controller-serviceaccount",
        "namespace": namespace.metadata["name"],
        "annotations": {
            "eks.amazonaws.com/role-arn": iam_role.arn.apply(lambda arn: arn)
        }
    }
)

# This transformation is needed to remove the status field from the CRD
# otherwise the Chart fails to deploy
def remove_status(obj, opts):
    if obj["kind"] == "CustomResourceDefinition":
        del obj["status"]

k8s.helm.v3.Chart(
    "lb", k8s.helm.v3.ChartOpts(
        chart="aws-load-balancer-controller",
        version="1.2.0",
        fetch_opts=k8s.helm.v3.FetchOpts(
            repo="https://aws.github.io/eks-charts"
        ),
        namespace=namespace.metadata["name"],
        values={
            "region": "ap-northeast-2",
            "serviceAccount": {
                "name": "aws-lb-controller-serviceaccount",
                "create": False,
            },
            "vpcId": cluster.eks_cluster.vpc_config.vpc_id,
            "clusterName": cluster.eks_cluster.name,
            "podLabels": {
                "stack": pulumi.get_stack(),
                "app": "aws-lb-controller"
            }
        },
        transformations=[remove_status]
    ), pulumi.ResourceOptions(
        provider=provider, parent=namespace
    )
)
