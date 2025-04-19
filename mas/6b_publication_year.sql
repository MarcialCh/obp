SELECT 
	publication1.year,
    save_circuit(provenance(), 'publication_id', '{path}')
FROM 
	author as author1, author as author2, writes as writes1, writes as writes2, publication as publication1, publication as publication2, conference 
WHERE
	author1.aid = writes1.aid
    AND writes1.pid = publication1.pid
    AND publication1.cid = conference.cid
    AND publication1.year > 2010
    AND author1.name = 'Tova Milo'
    AND author2.aid = writes2.aid
    AND writes2.pid = publication2.pid
    AND publication2.cid = conference.cid
    AND publication2.year > 2010    
    AND author2.name = 'H. V. Jagadish'
    AND publication1.year = publication2.year
GROUP BY publication1.year
LIMIT 10