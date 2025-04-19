SELECT 
	author.name,
    save_circuit(provenance(), 'author_id', '{path}') 
FROM 
	author, organization, writes, publication, conference, domain_author, domain_conference, domain as domain1, domain as domain2
WHERE
	author.oid = organization.oid
	AND author.aid = writes.aid
    AND writes.pid = publication.pid
    AND publication.cid = conference.cid
    AND conference.cid = domain_conference.cid
    AND domain_conference.did = domain1.did
    AND author.aid = domain_author.aid
    AND domain_author.did = domain2.did
    AND organization.name = 'Tel Aviv University'
    AND publication.year > 2000
    AND domain1.name = 'Machine Learning & Pattern Recognition'
    AND domain2.name != 'Machine Learning & Pattern Recognition'
GROUP BY author.name 
LIMIT 10